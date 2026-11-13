import functools
import glob
import html
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from services import bases_download, schedule

# pypdf avisa por cada diccionario repetido de los PDF del SEACE; no afecta la lectura
logging.getLogger("pypdf").setLevel(logging.ERROR)

READABLE_SUFFIXES = (".pdf", ".docx")
SCANNED_CHARS_PER_PAGE = 200      # menos en promedio = PDF escaneado (SP-005)
CHARS_PER_TOKEN = 4               # estimación del SP-005
SHORT_SECTION_TOKENS = 150        # menos suele ser una línea del índice (SP-006, 28/10)
MAX_SECTION_CHARS = 12000        # las secciones reales medidas llegan a ~7000; sin final claro se corta aquí
OCR_DPI = 150
OCR_MAX_PAGES = 160               # la base escaneada más larga del SP-005 tiene 137 páginas

INDEX_LEADERS = re.compile(r"\.{4,}|…{2,}|_{4,}")
NEXT_SECTION_PATTERN = re.compile(r"\n\s*(cap[ií]tulo|anexo|secci[oó]n)\s|\n\s*[0-9.]*\s*factores\s+de\s+evaluaci", re.I)
# "PROPUESTA DE": en algunas bases el requerimiento titula "10.1. PROPUESTA DE REQUISITOS DE CALIFICACIÓN"
# (hallado con --diagnosticar-bases en la base 1248982 del SP-005, página 32).
# Numeración con coma: el OCR lee "10." como "10," y "10.1" como "10,1" (base 1262730, página 33, 03/11/2026).
HEADING_PREFIX = re.compile(r"(^|\n)[ \t|\-–—()\[\]!_*•]*(cap[ií]tulo[ \t]+[ivx0-9]+[ \t]*[:.\-–]?[ \t]*)?"
                            r"([0-9]+([.,][0-9]+)*[.,]?|[a-z]\.|[ivx]+\.)?[ \t]*(propuesta[ \t]+de[ \t]+)?$", re.I)


def _spaced(word):
    """Tolera un espacio suelto entre letras ("R EQUISITOS"), como deja pypdf en algunos PDF."""
    return r"\s?".join(re.escape(ch) for ch in word)


REQUIREMENTS_PATTERN = re.compile(_spaced("requisitos") + r"\s+" + _spaced("de") + r"\s+"
                                  + "(" + _spaced("calificaci") + "|" + _spaced("habilitaci") + ")", re.I)


@dataclass
class Section:
    """Sección de requisitos encontrada: su texto y en qué página empieza cada carácter."""
    text: str
    document: str                       # nombre del archivo dentro de lo descargado
    source: str                         # "texto" u "ocr"
    page_marks: list = field(default_factory=list)   # [(posición en text, página)]; vacío en Word

    def page_at(self, position):
        page = None
        for start, number in self.page_marks:
            if start <= position:
                page = number
        return page


@dataclass
class ReadResult:
    status: str                          # "extraido", "seccion_no_encontrada" o "no_legible"
    section: Section | None = None
    detail: str = ""
    schedule: object = None              # schedule.ScheduleResult de los documentos leídos (HU-018), o None


# ---------------------------------------------------------------------------
# Abrir lo descargado
# ---------------------------------------------------------------------------
def _archive_tools():
    """Herramientas para RAR y 7z en orden de preferencia (prueba del SP-006, 27/10/2026)."""
    tools = []
    if shutil.which("bsdtar"):
        tools.append(lambda src, dst: ["bsdtar", "-xf", src, "-C", dst])
    elif shutil.which("tar"):
        try:
            version = subprocess.run(["tar", "--version"], capture_output=True, text=True, timeout=10).stdout.lower()
        except (OSError, subprocess.SubprocessError):
            version = ""
        if "bsdtar" in version or "libarchive" in version:      # macOS: tar es bsdtar
            tools.append(lambda src, dst: ["tar", "-xf", src, "-C", dst])
    if shutil.which("unar"):
        tools.append(lambda src, dst: ["unar", "-q", "-f", "-o", dst, src])
    if shutil.which("7z"):
        tools.append(lambda src, dst: ["7z", "x", "-y", f"-o{dst}", src])
    return tools


def _extract_archive(content, suffix):
    for command in _archive_tools():
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / f"archivo.{suffix}"
            source.write_bytes(content)
            out = Path(tmp) / "x"
            out.mkdir()
            try:
                subprocess.run(command(str(source), str(out)), capture_output=True, timeout=300)
            except (OSError, subprocess.SubprocessError):
                continue
            files = [(p.name, p.read_bytes()) for p in sorted(out.rglob("*"))
                     if p.is_file() and p.suffix.lower() in READABLE_SUFFIXES]
            files = [(n, b) for n, b in files if b]           # el 7z de Ubuntu deja archivos vacíos
            if files:
                return files
    return []


def documents_in(content):
    """[(nombre, bytes)] de los PDF y Word que hay en lo descargado."""
    kind = bases_download.detect_kind(content[:8192])
    if kind in ("docx", "xlsx", "zip"):
        # Los primeros bytes no bastan: un ZIP que trae un Word sin comprimir también dice "word/".
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                kind = "docx" if "word/document.xml" in archive.namelist() else "zip"
        except zipfile.BadZipFile:
            return []
    if kind == "pdf":
        return [("bases.pdf", content)]
    if kind == "docx":
        return [("bases.docx", content)]
    if kind == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                inner = [(Path(n).name, archive.read(n)) for n in archive.namelist()
                         if n.lower().endswith(READABLE_SUFFIXES) and not n.startswith("__MACOSX")]
        except zipfile.BadZipFile:
            return []
        return [(n, b) for n, b in inner if b]
    if kind in ("rar", "7z"):
        return _extract_archive(content, kind)
    return []


# ---------------------------------------------------------------------------
# Texto por página
# ---------------------------------------------------------------------------
def pdf_pages(content):
    return pdf_pages_info(content)[0]


BIG_IMAGE_SIDE = 400            # px: el logo y la franja lateral de las bases miden menos de un lado
IMAGE_PAGE_MAX_CHARS = 1000     # una página con imagen grande y menos texto que esto se lee con OCR


def _has_big_image(page):
    """True si la página trae una imagen de al menos 400 × 400 px (sin decodificarla)."""
    try:
        xobjects = page["/Resources"].get_object().get("/XObject")
        if not xobjects:
            return False
        for ref in xobjects.get_object().values():
            obj = ref.get_object()
            if (obj.get("/Subtype") == "/Image" and int(obj.get("/Width", 0)) >= BIG_IMAGE_SIDE
                    and int(obj.get("/Height", 0)) >= BIG_IMAGE_SIDE):
                return True
    except Exception:
        return False
    return False


def pdf_pages_info(content):
    """(texto de cada página, si cada página trae una imagen grande)."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    pages, big = [], []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:          # una página dañada no debe tumbar toda la base
            pages.append("")
        big.append(_has_big_image(page))
    return pages, big


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")
MIN_IMAGE_BYTES = 30_000         # menos suele ser un logo o un sello
MAX_WORD_IMAGES = 60


def docx_images(content):
    """Imágenes del Word en el orden en que aparecen en el documento: [(nombre, bytes)]."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
        try:
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8", errors="ignore")
        except KeyError:
            return []
        targets = {}
        for rel in re.findall(r"<Relationship\b[^>]*>", rels):
            rid = re.search(r'Id="([^"]+)"', rel)
            target = re.search(r'Target="([^"]+)"', rel)
            if rid and target:
                targets[rid.group(1)] = target.group(1)
        images, seen = [], set()
        for rid in re.findall(r'r:embed="([^"]+)"', xml):
            target = targets.get(rid, "")
            path = "word/" + target.lstrip("/") if not target.startswith("word/") else target
            if path in seen or not path.lower().endswith(IMAGE_SUFFIXES):
                continue
            seen.add(path)
            try:
                data = archive.read(path)
            except KeyError:
                continue
            if len(data) >= MIN_IMAGE_BYTES:
                images.append((Path(path).name, data))
        return images[:MAX_WORD_IMAGES]


def ocr_image(name, data):
    """Texto de una imagen suelta (la que trae un Word) con tesseract en español."""
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / Path(name).name
        image.write_bytes(data)
        text = _tesseract(image, "3")
        if _spanish_score(text) < 0.14:
            retry = _tesseract(image, "1")
            if _spanish_score(retry) > _spanish_score(text):
                text = retry
        return text


def docx_text(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


COMMON_WORDS = {"de", "la", "el", "en", "que", "los", "del", "y", "las", "por", "con", "se", "a", "o",
                "para", "su", "al", "un", "una", "no", "es"}
OCR_ORDINAL = re.compile(r"(?<![0-9])(?<![Nn])º|(?<=[Nn])º(?!\s?\d)")


def _spanish_score(text):
    words = re.findall(r"[a-záéíóúñ]+", text.lower())
    return sum(w in COMMON_WORDS for w in words) / len(words) if words else 0.0


# Dónde suelen quedar en Windows si no se agregaron al PATH (instalador de UB Mannheim y ZIP de Poppler)
WINDOWS_TESSERACT = [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe")]
WINDOWS_POPPLER = [r"C:\poppler\*\Library\bin\pdftoppm.exe", r"C:\poppler\Library\bin\pdftoppm.exe",
                   r"C:\Program Files\poppler*\Library\bin\pdftoppm.exe", r"C:\Program Files\poppler*\bin\pdftoppm.exe",
                   os.path.join(os.environ.get("USERPROFILE", ""), "poppler*", "Library", "bin", "pdftoppm.exe")]


@functools.lru_cache(maxsize=None)
def tool_path(name):
    """Ruta del programa externo ("tesseract" o "pdftoppm"), o None si no está.

    Orden: la variable TESSERACT_PATH / POPPLER_PATH del .env (el .exe o su carpeta), el PATH y, en
    Windows, las carpetas donde suelen instalarse. Así el OCR funciona en Windows aunque no se haya
    tocado el PATH.
    """
    variable = {"tesseract": "TESSERACT_PATH", "pdftoppm": "POPPLER_PATH"}.get(name)
    configured = os.environ.get(variable, "").strip().strip('"') if variable else ""
    if configured:
        for candidate in (configured, os.path.join(configured, name), os.path.join(configured, name + ".exe")):
            if os.path.isfile(candidate):
                return candidate
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        patterns = WINDOWS_TESSERACT if name == "tesseract" else WINDOWS_POPPLER if name == "pdftoppm" else []
        for pattern in patterns:
            matches = sorted(glob.glob(pattern), reverse=True)      # la versión más nueva primero
            if matches:
                return matches[0]
    return None


def ocr_available():
    tesseract, pdftoppm = tool_path("tesseract"), tool_path("pdftoppm")
    if not (tesseract and pdftoppm):
        return False
    try:
        langs = subprocess.run([tesseract, "--list-langs"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "spa" in (langs.stdout + langs.stderr).split()


ENUMERATION = re.compile(r"[A-Za-z0-9]{1,2}[.)]")


def clean_ocr(text):
    """Quita las líneas sueltas de 1 a 4 caracteres que deja el OCR de sellos, firmas y franjas
    laterales ("1]", "(5)", "fo]"), salvo enumeraciones como "A." o "2)"."""
    kept = []
    for line in text.split("\n"):
        short = line.strip()
        if short and len(short) <= 4 and not ENUMERATION.fullmatch(short):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept))


def _tesseract(image, psm):
    result = subprocess.run([tool_path("tesseract") or "tesseract", str(image), "stdout", "-l", "spa", "--psm", psm],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    return clean_ocr(OCR_ORDINAL.sub("o", result.stdout)) if result.returncode == 0 else ""


def ocr_pages(content, page_numbers):
    """Texto de las páginas indicadas (desde 1) con pdftoppm + tesseract en español."""
    texts = {}
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "doc.pdf"
        pdf.write_bytes(content)
        for n in page_numbers:
            prefix = Path(tmp) / f"p{n:04d}"
            subprocess.run([tool_path("pdftoppm") or "pdftoppm", "-r", str(OCR_DPI), "-gray", "-f", str(n), "-l", str(n),
                            str(pdf), str(prefix)],
                           capture_output=True, timeout=300)
            images = sorted(Path(tmp).glob(f"{prefix.name}*.pgm"))
            text = _tesseract(images[0], "3") if images else ""
            if images and _spanish_score(text) < 0.14:       # página girada (SP-006): reintenta con --psm 1
                retry = _tesseract(images[0], "1")
                if _spanish_score(retry) > _spanish_score(text):
                    text = retry
            texts[n] = text
    return texts


# ---------------------------------------------------------------------------
# Sección de requisitos
# ---------------------------------------------------------------------------
def is_heading(text, start):
    """Título = la frase empieza la línea y va en mayúsculas o con numeración ("3.6")."""
    line_start = text.rfind("\n", 0, start) + 1
    match = HEADING_PREFIX.match("\n" + text[line_start:start])
    if not match:
        return False
    numbered = bool(match.group(2) or match.group(3))
    return numbered or text[start:start + 25].upper() == text[start:start + 25]


# En las subastas inversas el requerimiento trae un numeral "15. HABILITACIONES" (o "15, HABILITACIONES"
# con OCR) con la inscripción en OSINERGMIN y su acreditación (base 1262142, página 27, 03/11/2026).
HABILITATION_HEADING = re.compile(r"(?:^|\n)[ \t|]*[0-9]{1,2}[.,]?[ \t]+(HABILITACI[OÓ]N(?:ES)?)[ \t—–\-]*(?=\n|$)")
NEXT_NUMBERED_ITEM = re.compile(r"\n[ \t|]*[0-9]{1,2}[.,]?[ \t]+[A-ZÁÉÍÓÚÑ]{4,}")
MAX_HABILITATION_CHARS = 6000


def _habilitation_candidates(text):
    found = []
    for match in HABILITATION_HEADING.finditer(text):
        start = match.start(1)
        rest = text[start:]
        ends = [m.start() for m in (NEXT_NUMBERED_ITEM.search(rest, 20), NEXT_SECTION_PATTERN.search(rest, 20)) if m]
        length = min(ends + [MAX_HABILITATION_CHARS, len(rest)])
        verdict = "corta" if length / CHARS_PER_TOKEN < SHORT_SECTION_TOKENS else "seccion"
        found.append({"start": start, "end": start + length, "verdict": verdict, "kind": "habilitaciones"})
    return found


def candidates(text):
    """Cada aparición de "requisitos de calificación/habilitación" con su veredicto.

    Veredictos: "seccion" (se usa), "mencion" (dentro de un párrafo, no es título),
    "indice" (línea del índice con puntos guía) y "corta" (menos de ~600 caracteres).
    """
    found = []
    for match in REQUIREMENTS_PATTERN.finditer(text):
        rest = text[match.start():]
        end = NEXT_SECTION_PATTERN.search(rest, 200)
        length = min(end.start() if end else len(rest), MAX_SECTION_CHARS)
        body = rest[:length]
        if not is_heading(text, match.start()):
            verdict = "mencion"
        elif INDEX_LEADERS.search(body[:300]):
            verdict = "indice"
        elif len(body) / CHARS_PER_TOKEN < SHORT_SECTION_TOKENS:
            verdict = "corta"
        else:
            verdict = "seccion"
        found.append({"start": match.start(), "end": match.start() + length, "verdict": verdict, "kind": "requisitos"})
    return found + _habilitation_candidates(text)


def best_section(text):
    """(inicio, fin) de la sección útil: título de sección, no línea del índice, la más larga.

    "Requisitos de calificación/habilitación" tiene prioridad; "HABILITACIONES" solo se usa si no hay otra.
    """
    found = candidates(text)
    for kind in ("requisitos", "habilitaciones"):
        useful = [(c["start"], c["end"]) for c in found if c["verdict"] == "seccion" and c["kind"] == kind]
        if useful:
            return max(useful, key=lambda c: c[1] - c[0])
    return None


@dataclass
class DocumentText:
    """Texto de un documento de las bases, con el carácter donde empieza cada página."""
    name: str
    text: str
    starts: list                 # posición de inicio de cada página; vacío en Word
    pages: int = 0
    image_pages: list = field(default_factory=list)   # páginas sin texto (escaneadas)
    ocr_pages: set = field(default_factory=set)       # páginas leídas con OCR
    unread_scan: bool = False    # escaneado que no se pudo leer (sin OCR o demasiado largo)
    image_text_from: int | None = None   # en Word: desde dónde el texto sale de las imágenes (OCR)

    def page_of(self, position):
        page = None
        for i, start in enumerate(self.starts):
            if start <= position:
                page = i + 1
        return page


def document_text(name, data, use_ocr, word_images=False):
    """DocumentText de un PDF o Word, o None si no se puede abrir.

    word_images: en un Word, además lee con OCR sus imágenes (algunas entidades pegan el Capítulo III
    como fotos: base 1255879, 03/11/2026). El texto de las imágenes va al final, en su orden.
    """
    if name.lower().endswith(".docx"):
        try:
            text = docx_text(data)
            doc = DocumentText(name=name, text=text, starts=[])
            if word_images and use_ocr:
                images = docx_images(data)
                if images:
                    doc.image_text_from = len(text)
                    doc.text = text + "\n" + "\n".join(ocr_image(n, b) for n, b in images)
                    doc.image_pages = list(range(1, len(images) + 1))
            return doc
        except (zipfile.BadZipFile, KeyError):
            return None
    try:
        pages, big_image = pdf_pages_info(data)
    except Exception:
        return None
    if not pages:
        return None
    # Escaneada = casi sin texto, o una imagen grande con solo el encabezado en texto (base 1258549:
    # el Capítulo III son fotos con 212 caracteres de encabezado, apenas sobre el límite de 200)
    image_pages = [i + 1 for i, p in enumerate(pages)
                   if len(p.strip()) < SCANNED_CHARS_PER_PAGE
                   or (big_image[i] and len(p.strip()) < IMAGE_PAGE_MAX_CHARS)]
    scanned = sum(len(p.strip()) for p in pages) / len(pages) < SCANNED_CHARS_PER_PAGE
    ocr_done = set()
    if image_pages and use_ocr and len(image_pages) <= OCR_MAX_PAGES:
        # Escaneado completo, o PDF con texto que trae escaneadas justo las páginas del
        # requerimiento (pasó en 4 de las bases del SP-005): solo esas páginas pasan por OCR
        for number, page_text in ocr_pages(data, image_pages).items():
            pages[number - 1] = page_text
        ocr_done = set(image_pages)
        scanned = False
    starts, pos = [], 0
    for page in pages:
        starts.append(pos)
        pos += len(page) + 1
    return DocumentText(name=name, text="\n".join(pages), starts=starts, pages=len(pages),
                        image_pages=image_pages, ocr_pages=ocr_done, unread_scan=scanned)


def _section_of(doc):
    found = best_section(doc.text)
    if found and doc.starts and doc.ocr_pages:
        # Si la sección existe en páginas con texto, se prefiere a la que salió del OCR (más limpia):
        # en 1250704 y 1257132 el requerimiento repite la sección como foto, con firmas y sellos.
        text_only = [c for c in candidates(doc.text) if c["verdict"] == "seccion"
                     and doc.page_of(c["start"]) not in doc.ocr_pages]
        if text_only:
            best_kind = min(("requisitos", "habilitaciones").index(c["kind"]) for c in text_only)
            pick = max((c for c in text_only if ("requisitos", "habilitaciones").index(c["kind"]) == best_kind),
                       key=lambda c: c["end"] - c["start"])
            found = (pick["start"], pick["end"])
    if not found:
        return None
    begin, end = found
    if not doc.starts:
        from_images = doc.image_text_from is not None and begin >= doc.image_text_from
        return Section(text=doc.text[begin:end], document=doc.name, source="ocr" if from_images else "texto")
    starts = doc.starts
    marks = [(max(0, s - begin), i + 1) for i, s in enumerate(starts)
             if s < end and (i + 1 == len(starts) or starts[i + 1] > begin)]
    source = "ocr" if any(number in doc.ocr_pages for _, number in marks) else "texto"
    return Section(text=doc.text[begin:end], document=doc.name, source=source, page_marks=marks)


def read_bases(content, use_ocr=None):
    """Lee lo descargado y devuelve ReadResult con la sección de requisitos, si la hay."""
    use_ocr = ocr_available() if use_ocr is None else use_ocr
    documents = documents_in(content)
    if not documents:
        kind = bases_download.detect_kind(content[:8192])
        return ReadResult("no_legible", detail=f"no se pudo abrir el archivo ({kind or 'formato desconocido'})")
    readable = False
    unread_scans = []
    found_schedule = None        # HU-018: el cronograma se busca en cada documento que se abre
    for name, data in documents:
        doc = document_text(name, data, use_ocr)
        if doc is None:
            continue
        if doc.unread_scan:
            unread_scans.append(len(doc.image_pages))
            continue
        readable = readable or len(doc.text.strip()) >= SCANNED_CHARS_PER_PAGE
        found_schedule = schedule.better(found_schedule, schedule.schedule_of_document(doc))
        section = _section_of(doc)
        if section:
            return ReadResult("extraido", section, schedule=found_schedule)
    if use_ocr:
        # Segunda pasada, solo si ningún documento trae la sección: OCR de las imágenes de los Word
        # (en 1248982 el Word trae fotos de otra cosa y el PDF sí tenía la sección)
        for name, data in documents:
            if name.lower().endswith(".docx"):
                with_images = document_text(name, data, use_ocr, word_images=True)
                section = _section_of(with_images) if with_images else None
                if section:
                    return ReadResult("extraido", section, schedule=found_schedule)
    if readable:
        return ReadResult("seccion_no_encontrada", detail=f"se leyeron {len(documents)} documento(s) sin la sección",
                          schedule=found_schedule)
    if unread_scans and use_ocr:
        detail = f"PDF escaneado de {max(unread_scans)} páginas: supera el límite de OCR ({OCR_MAX_PAGES})"
    elif unread_scans:
        detail = "PDF escaneado y no hay OCR instalado (tesseract con español y pdftoppm)"
    else:
        detail = "sin texto legible"
    return ReadResult("no_legible", detail=detail, schedule=found_schedule)


def read_schedule(content):
    """Solo el cronograma (HU-018), sin OCR: lo usa la ingesta cuando cambia la etapa de una convocatoria
    cuyas bases ya se leyeron, o cuando el cronograma aún no se leyó. Devuelve un schedule.ScheduleResult."""
    found = None
    for name, data in documents_in(content):
        doc = document_text(name, data, use_ocr=False)
        if doc is not None and not doc.unread_scan:
            found = schedule.better(found, schedule.schedule_of_document(doc))
    return found or schedule.ScheduleResult(schedule.NO_LEGIBLE, detail="las bases no tienen texto legible sin OCR")


# ---------------------------------------------------------------------------
# Diagnóstico de las bases sin sección (no guarda nada)
# ---------------------------------------------------------------------------
QUALIFY_LINE = re.compile(r"califica|habilitaci", re.I)
VERDICT_TEXT = {
    "seccion": "título reconocido (se usaría)",
    "mencion": "mención dentro de un párrafo: no empieza la línea, o no va en mayúsculas ni numerada",
    "indice": "línea del índice (puntos guía hacia un número de página)",
    "corta": "título reconocido, pero la sección mide menos de ~600 caracteres",
    "otra": "dice «califica/habilitación» sin la frase «requisitos de …» (otro título u OCR deformado)",
}


ORDER = {"corta": 0, "otra": 1, "indice": 2, "seccion": 3, "mencion": 4}
NUMBERING = re.compile(r"^([0-9]+([.,][0-9]+)*[.,]?|[a-zA-Z][.)]|[ivxIVX]+\.)\s")


def looks_like_title(line):
    """Línea corta, sin punto final, en mayúsculas o numerada: puede ser un título con otra redacción."""
    letters = [c for c in line if c.isalpha()]
    upper = sum(c.isupper() for c in letters) / len(letters) if letters else 0
    return len(line) <= 90 and not line.endswith((".", ",", ";")) and (upper >= 0.6 or bool(NUMBERING.match(line)))


def explain(content, use_ocr=None, max_lines=12):
    """Por qué no se encontró la sección: documentos leídos y cada línea que menciona la calificación.

    Devuelve [{"document", "pages", "image_pages", "ocr_pages", "unread_scan", "lines": [(página, línea, veredicto)]}].
    """
    use_ocr = ocr_available() if use_ocr is None else use_ocr
    report = []
    for name, data in documents_in(content):
        doc = document_text(name, data, use_ocr, word_images=True)
        if doc is None:
            report.append({"document": name, "error": "no se pudo abrir"})
            continue
        verdict_at = {c["start"]: c for c in candidates(doc.text)}
        lines, seen = [], set()
        for match in QUALIFY_LINE.finditer(doc.text):
            line_start = doc.text.rfind("\n", 0, match.start()) + 1
            if line_start in seen:
                continue
            seen.add(line_start)
            line_end = doc.text.find("\n", match.start())
            line_end = len(doc.text) if line_end == -1 else line_end
            line = re.sub(r"\s+", " ", doc.text[line_start:line_end]).strip()[:140]
            hit = next((c for start, c in verdict_at.items() if line_start <= start < line_end), None)
            verdict = hit["verdict"] if hit else "otra"
            if verdict == "otra" and not looks_like_title(line):
                continue                     # un párrafo cualquiera que dice "calificación": no ayuda
            if hit and verdict == "corta":
                verdict_text = f"{VERDICT_TEXT['corta']} ({hit['end'] - hit['start']} caracteres)"
            else:
                verdict_text = VERDICT_TEXT[verdict]
            lines.append((ORDER[verdict], doc.page_of(match.start()), line, verdict_text))
        # Primero las que más ayudan a corregir el lector: títulos cortos o con otra redacción
        lines = [l[1:] for l in sorted(lines, key=lambda l: l[0])]
        report.append({"document": name, "pages": doc.pages, "image_pages": len(doc.image_pages),
                       "ocr_pages": len(doc.ocr_pages), "unread_scan": doc.unread_scan,
                       "total_lines": len(seen), "lines": lines[:max_lines]})
    return report