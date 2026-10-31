"""SP-006 · Lectura de las bases que descargó el SP-005 (actividades 1, 3, 4 y 6).

Trabaja con las bases que ya están en salidas/sp005/bases/; no descarga nada.
Para cada base elegida (por defecto, 3 escaneadas y 10 con texto de los rubros 1510 y 2510):

  - abre el documento aunque llegue en PDF, Word, ZIP o RAR;
  - decide si tiene texto o es escaneado (menos de 200 caracteres por página);
  - con --ocr, lee las escaneadas con OCR (tesseract en español), sin usar Gemini;
  - ubica TODAS las apariciones de "requisitos de calificación", guarda la sección más
    larga con su página y avisa si es tan corta que parece una entrada del índice;
  - busca en todo el texto el valor estimado (o referencial) y el cronograma con la
    presentación de ofertas.

Salidas en salidas/sp006/:
  bases_sp006.csv         una fila por base, con lo medido
  secciones/<código>.txt  la sección de requisitos (cuando es un título), para armar el catálogo
  secciones/<código>_corta.txt   si el título está pero la sección es corta (revisar: índice o sección)
  secciones/<código>_mencion.txt  si solo hubo menciones dentro de párrafos (no son requisitos)
  catalogo_requisitos.csv plantilla del catálogo de requisitos (se llena a mano)
  rar_linux.csv           con --rar-check: qué herramienta abre cada RAR

Uso, desde analisis/:
    python sp006_01_leer_bases.py                       las 13 bases sugeridas
    python sp006_01_leer_bases.py --ocr                 además, OCR de las escaneadas
    python sp006_01_leer_bases.py --codigos 1258029,1257402
    python sp006_01_leer_bases.py --rar-check           solo prueba los RAR con cada herramienta
    python sp006_01_leer_bases.py --diagnostico 1257868 líneas con "califica" de una base, con su página

Este script es parte de un spike: no forma parte de la app.
"""
import argparse
import glob
import io
import os
import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import pandas as pd
from pypdf import PdfReader

import sp005_01_tender_source as sp

ROOT = Path(__file__).resolve().parent
SP005_DIR = sp.OUTPUT_DIR                       # salidas/sp005 (o SP005_OUTPUT_DIR)
OUTPUT_DIR = ROOT / "salidas" / "sp006"

# Selección sugerida (actividad 1). Sale de documentos.csv del 17/10 cruzado con vigentes.json:
# cubre los dos rubros, todos los tipos de procedimiento de los pilotos salvo uno, los cuatro
# formatos y los casos dudosos del SP-005 (sección muy corta o no encontrada).
SUGGESTED_SCANNED = {
    "1255745": "1510, Subasta Inversa Electrónica, PDF escaneado de 62 páginas (la demo del SP-005)",
    "1253677": "1510, Licitación Pública Abreviada, Bases Integradas escaneadas de 81 páginas",
    "1258393": "2510, Licitación Pública, PDF escaneado de 81 páginas",
}
SUGGESTED_TEXT = {
    "1255636": "2510, Licitación Pública Abreviada, llegó en ZIP",
    "1257402": "2510, Licitación Pública, llegó en RAR; el SP-005 halló una sección de solo 87 tokens",
    "1258093": "2510, Comparación de Precios",
    "1254843": "1510, Subasta Inversa Electrónica, llegó en Word",
    "1250704": "1510, Licitación Pública Abreviada, Bases Integradas; sección de solo 273 tokens",
    "1257702": "1510, Licitación Pública Abreviada, llegó en Word",
    "1258420": "1510, Contratación Directa",
    "1257868": "1510, Subasta Inversa Electrónica, PDF",
    "1258127": "1510, Subasta Inversa Electrónica, llegó en ZIP",
    "1258029": "1510, Subasta Inversa Electrónica, llegó en RAR; el SP-005 no halló la sección",
}

# Menos que esto suele ser una línea del índice (el SP-005 halló una de 87 tokens en 1257402). Era 300,
# pero el 28/10 aparecieron secciones reales más cortas: 1258127 (~266 tokens) y 1258029 (~276).
SHORT_SECTION_TOKENS = 150
INDEX_LEADERS = re.compile(r"\.{4,}|…{2,}|_{4,}")   # "Requisitos de calificación ........ 12"
OCR_DPI = 150

VALUE_PATTERN = re.compile(r"valor\s+(estimado|referencial)", re.I)
AMOUNT_PATTERN = re.compile(r"S/\.?\s*\d[\d.,\s]*\d", re.I)
SCHEDULE_PATTERN = re.compile(r"cronograma", re.I)
OFFERS_PATTERN = re.compile(r"presentaci[oó]n\s+(de\s+)?(ofertas|propuestas)", re.I)
DATE_PATTERN = re.compile(r"\b\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}\b")

CATALOG_COLUMNS = ["tipo_requisito", "como_aparece_escrito", "aplica_a_bienes", "campo_del_perfil",
                   "rubros", "metodos", "bases_donde_aparece", "pagina_ejemplo", "observaciones"]
# Tipos candidatos (documento del 12/10, fuentes secundarias): se confirman o se quitan al leer las bases.
CANDIDATE_TYPES = ["Habilitación legal", "Experiencia del postor en bienes iguales o similares",
                   "Equipamiento estratégico", "Personal clave", "Plazo de entrega", "Garantías"]


# ---------------------------------------------------------------------------
# 1. Abrir el documento: PDF, Word, ZIP o RAR
# ---------------------------------------------------------------------------
def rar_version(content):
    """RAR5 empieza con Rar!\\x1a\\x07\\x01\\x00; RAR4 (y anteriores) con Rar!\\x1a\\x07\\x00."""
    if content[:8] == b"Rar!\x1a\x07\x01\x00":
        return "RAR5"
    if content[:7] == b"Rar!\x1a\x07\x00":
        return "RAR4"
    return "desconocido"


def _tar_is_bsdtar():
    """En macOS y Windows, "tar" es bsdtar (libarchive) y abre RAR; en Linux suele ser GNU tar, que no."""
    if not shutil.which("tar"):
        return False
    try:
        out = subprocess.run(["tar", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "bsdtar" in out.lower() or "libarchive" in out.lower()


def extractors():
    """Herramientas disponibles para abrir RAR, en orden de preferencia.

    Prueba hecha el 27/10/2026 en Ubuntu 24.04 con archivos de ejemplo RAR4 y RAR5:
    bsdtar (libarchive-tools) abre bien RAR5 y falla en algunos RAR4; unar abre bien RAR4
    y falla en algunos RAR5; el 7z de Ubuntu ("7zip", versión dfsg) deja vacíos los archivos comprimidos.
    Por eso se usa bsdtar y, si falla, unar. 7z queda solo como último recurso y se verifica
    que lo extraído no esté vacío.
    """
    tools = []
    if shutil.which("bsdtar"):
        tools.append(("bsdtar", lambda src, dst: ["bsdtar", "-xf", src, "-C", dst]))
    elif _tar_is_bsdtar():
        tools.append(("tar (bsdtar)", lambda src, dst: ["tar", "-xf", src, "-C", dst]))
    if shutil.which("unar"):
        tools.append(("unar", lambda src, dst: ["unar", "-q", "-f", "-o", dst, src]))
    if shutil.which("7z"):
        tools.append(("7z", lambda src, dst: ["7z", "x", "-y", f"-o{dst}", src]))
    return tools


def extract_with(tool_cmd, content, suffix):
    """Extrae un comprimido con una herramienta; devuelve [(nombre, bytes)] de los PDF y Word no vacíos."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"archivo.{suffix}"
        source.write_bytes(content)
        out = Path(tmp) / "x"
        out.mkdir()
        result = subprocess.run(tool_cmd(str(source), str(out)), capture_output=True, timeout=300)
        files = [(p.name, p.read_bytes()) for p in out.rglob("*")
                 if p.is_file() and p.suffix.lower() in sp.READABLE_SUFFIXES]
        files = [(n, b) for n, b in files if len(b) > 0]   # el 7z sin RAR deja archivos vacíos
        return result.returncode, files


def documents_in(content, kind):
    """Documentos legibles dentro de lo descargado y la herramienta que los abrió."""
    if kind in ("pdf", "docx", "zip"):
        return sp.pdfs_inside(content, kind), "python"
    if kind in ("rar", "7z"):
        errors = []
        for name, cmd in extractors():
            code, files = extract_with(cmd, content, kind)
            if files:
                return files, name
            errors.append(f"{name}: código {code}, sin PDF ni Word")
        raise RuntimeError("no se pudo abrir el " + kind + ("; " + "; ".join(errors) if errors else
                           "; no hay bsdtar, unar ni 7z instalados"))
    return [], None


# ---------------------------------------------------------------------------
# 2. Texto por página (con OCR opcional para las escaneadas)
# ---------------------------------------------------------------------------
def pdf_pages_text(content):
    reader = PdfReader(io.BytesIO(content))
    return [(page.extract_text() or "") for page in reader.pages]


def _docx_plain(content):
    """Texto de un Word (.docx) leyendo word/document.xml, como en el SP-005."""
    import html
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


# Dónde suelen quedar los programas en Windows si no están en el PATH. También se puede indicar la
# ruta exacta con las variables de entorno SP006_TESSERACT y SP006_PDFTOPPM.
TESSERACT_GUESSES = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                     r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
PDFTOPPM_GUESSES = [r"C:\poppler*\**\pdftoppm.exe", r"C:\Program Files\poppler*\**\pdftoppm.exe"]


def find_tool(name, env, guesses):
    """Ruta del programa: variable de entorno, PATH o ubicación habitual en Windows."""
    if os.environ.get(env) and Path(os.environ[env]).exists():
        return os.environ[env]
    if shutil.which(name):
        return shutil.which(name)
    for pattern in guesses:
        found = sorted(glob.glob(pattern, recursive=True))
        if found:
            return found[0]
    return None


def ocr_tools():
    """(pdftoppm, tesseract) listos para OCR en español, o (None, motivo)."""
    pdftoppm = find_tool("pdftoppm", "SP006_PDFTOPPM", PDFTOPPM_GUESSES)
    tesseract = find_tool("tesseract", "SP006_TESSERACT", TESSERACT_GUESSES)
    if not pdftoppm:
        return None, "no se encontró pdftoppm (Poppler)"
    if not tesseract:
        return None, "no se encontró tesseract"
    langs = subprocess.run([tesseract, "--list-langs"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    if "spa" not in (langs.stdout + langs.stderr).split():
        return None, "tesseract no tiene el idioma español (spa)"
    return (pdftoppm, tesseract), None


def ocr_available():
    tools, reason = ocr_tools()
    if reason:
        print(f"Aviso: {reason}.")
    return tools is not None


def ocr_pdf(content, page_numbers=None):
    """Texto de un PDF escaneado, página por página, con pdftoppm + tesseract (español).

    Con page_numbers (desde 1) lee solo esas páginas, en ese orden: sirve para los PDF con texto que
    traen algunas páginas escaneadas (en 1257868 y 1258420, el capítulo del requerimiento).
    """
    (pdftoppm, tesseract), _ = ocr_tools()
    pages = []
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "doc.pdf"
        pdf.write_bytes(content)
        if page_numbers:
            images = []
            for n in page_numbers:
                prefix = Path(tmp) / f"p{n:04d}"
                subprocess.run([pdftoppm, "-r", str(OCR_DPI), "-gray", "-f", str(n), "-l", str(n), str(pdf), str(prefix)],
                               check=True, capture_output=True, timeout=300)
                images += sorted(Path(tmp).glob(f"{prefix.name}*.pgm"))
        else:
            subprocess.run([pdftoppm, "-r", str(OCR_DPI), "-gray", str(pdf), str(Path(tmp) / "pg")],
                           check=True, capture_output=True, timeout=1800)
            images = sorted(Path(tmp).glob("pg-*.pgm"))
        for image in images:
            text = normalize_ocr(_tesseract(tesseract, image, "3"))
            if spanish_score(text) < ROTATED_PAGE_SCORE:
                # Página girada: en el RAR de 1258029 cada hoja trae dos páginas giradas 90° y el OCR
                # normal da texto ilegible. "--psm 1" detecta la orientación (usa osd.traineddata);
                # si falla o no mejora, queda el primer texto.
                retry = normalize_ocr(_tesseract(tesseract, image, "1"))
                if spanish_score(retry) > spanish_score(text):
                    text = retry
            pages.append(text)
    return pages


def _tesseract(tesseract, image, psm):
    # encoding="utf-8": tesseract escribe UTF-8; en Windows, sin esto, las tildes salen dañadas
    result = subprocess.run([tesseract, str(image), "stdout", "-l", "spa", "--psm", psm],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    return result.stdout if result.returncode == 0 else ""


# Proporción de palabras comunes del español ("de", "la", "que"...). Medido el 28/10: las 62 páginas
# de 1255745 dan 0.16 o más; las 34 hojas giradas de 1258029 dan de 0.05 a 0.12.
COMMON_WORDS = {"de", "la", "el", "en", "que", "los", "del", "y", "las", "por", "con", "se", "a", "o",
                "para", "su", "al", "un", "una", "no", "es"}
ROTATED_PAGE_SCORE = 0.14


def spanish_score(text):
    words = re.findall(r"[a-záéíóúñ]+", text.lower())
    return sum(w in COMMON_WORDS for w in words) / len(words) if words else 0.0


# Tesseract (spa) lee muchas "o" minúsculas como "º": "El pºstºr", "Requisitºs", "crºnºgrama".
# Con eso no se encuentran "requisitos" ni "cronograma" (en el RAR de 1258029, 0 apariciones en
# 34 páginas). Se cambia "º" por "o", salvo en "Nº 11" / "Nº09" (número) y "1º" (ordinal o nota).
OCR_ORDINAL = re.compile(r"(?<![0-9])(?<![Nn])º|(?<=[Nn])º(?!\s?\d)")


def normalize_ocr(text):
    return OCR_ORDINAL.sub("o", text)


# ---------------------------------------------------------------------------
# 3. Sección de requisitos, valor estimado y cronograma
# ---------------------------------------------------------------------------
def join_pages(pages):
    """Une las páginas y devuelve el texto y el carácter donde empieza cada página."""
    starts, parts, pos = [], [], 0
    for text in pages:
        starts.append(pos)
        parts.append(text)
        pos += len(text) + 1
    return "\n".join(parts), starts


def page_of(position, starts):
    """Página (desde 1) que contiene el carácter, o None si no hay páginas (Word)."""
    if not starts:
        return None
    page = 1
    for i, start in enumerate(starts):
        if start <= position:
            page = i + 1
    return page


# Un título de sección: la frase al inicio de la línea, con o sin numeración ("3.6 REQUISITOS DE
# CALIFICACIÓN"). Las menciones dentro de un párrafo ("...las bases y los requisitos de calificación...")
# no son títulos: la prueba con OCR del 27/10 mostró tres de ellas antes de la sección real.
# Antes del título se toleran restos de bordes de tabla que deja el OCR ("-) | 5.19 REQUISITOS DE
# HABILITACIÓN" en 1257868): barras, guiones, paréntesis y corchetes sueltos.
HEADING_PREFIX = re.compile(r"(^|\n)[ \t|\-–—()\[\]!_*•]*(cap[ií]tulo[ \t]+[ivx0-9]+[ \t]*[:.\-–]?[ \t]*)?"
                            r"([0-9]+(\.[0-9]+)*\.?|[a-z]\.|[ivx]+\.)?[ \t]*$", re.I)


def is_heading(text, start):
    """Título = la frase empieza la línea y además va en mayúsculas o lleva numeración ("3.6").

    Solo "empieza la línea" no basta: en el texto de un escaneado (OCR) los párrafos se cortan
    en líneas, y "requisitos de calificación se realiza..." también queda al inicio de una línea.
    """
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    match = HEADING_PREFIX.match("\n" + prefix)
    if not match:
        return False
    numbered = bool(match.group(2) or match.group(3))   # capítulo o numeración, no solo bordes de tabla
    upper = text[start:start + 25].upper() == text[start:start + 25]
    return numbered or upper


def _spaced(word):
    """Patrón de una palabra que tolera un espacio suelto entre letras ("R EQUISITOS", "calificació n"),
    como deja pypdf en algunos PDF (en las bases del 28/10 aparecen "SEA CE", "obt uvieron", "d os")."""
    return r"\s?".join(re.escape(ch) for ch in word)


# "Requisitos de habilitación": título que usa la ficha técnica de 1258029 (subasta inversa, MP de Ilo)
# para la inscripción en OSINERGMIN y en Bienes Fiscalizados; sus bases escaneadas no traen la sección.
REQUIREMENTS_PATTERN = re.compile(_spaced("requisitos") + r"\s+" + _spaced("de") + r"\s+"
                                  + "(" + _spaced("calificaci") + "|" + _spaced("habilitaci") + ")", re.I)
QUALIFY_WORD = re.compile(_spaced("califica") + "|" + _spaced("habilitaci"), re.I)


def requirement_sections(text):
    """Todas las secciones que empiezan en "requisitos de calificación" (o "de habilitación").

    Orden: primero las que son título de sección, y entre ellas la más larga; después las menciones
    dentro de un párrafo. Así no se toma un párrafo largo que solo menciona los requisitos.
    """
    sections = []
    for match in REQUIREMENTS_PATTERN.finditer(text):
        rest = text[match.start():]
        end = sp.NEXT_SECTION_PATTERN.search(rest, 200)
        body = rest[: end.start()] if end else rest[:20000]
        sections.append({"start": match.start(), "text": body, "titulo": is_heading(text, match.start())})
    return sorted(sections, key=lambda s: (s["titulo"], len(s["text"])), reverse=True)


def snippets(text, pattern, starts, width=220, limit=3):
    """Hasta 3 fragmentos donde aparece el patrón, con su página."""
    found = []
    for match in pattern.finditer(text):
        piece = re.sub(r"\s+", " ", text[match.start(): match.start() + width]).strip()
        found.append({"pagina": page_of(match.start(), starts), "texto": piece})
        if len(found) == limit:
            break
    return found


def value_and_schedule(text, starts):
    values = snippets(text, VALUE_PATTERN, starts)
    with_amount = [v for v in values if AMOUNT_PATTERN.search(v["texto"])]
    schedule = snippets(text, SCHEDULE_PATTERN, starts)
    offers = snippets(text, OFFERS_PATTERN, starts, width=160, limit=5)
    offers_with_date = [o for o in offers if DATE_PATTERN.search(o["texto"])]
    return {
        "valor_mencionado": bool(values),
        "valor_con_monto": bool(with_amount),
        "valor_fragmento": (with_amount or values or [{}])[0].get("texto", ""),
        "valor_pagina": (with_amount or values or [{}])[0].get("pagina"),
        "cronograma_mencionado": bool(schedule),
        "presentacion_ofertas_con_fecha": bool(offers_with_date),
        "ofertas_fragmento": (offers_with_date or offers or [{}])[0].get("texto", ""),
        "ofertas_pagina": (offers_with_date or offers or [{}])[0].get("pagina"),
    }


def analyze_document(name, content, use_ocr):
    """Mide un PDF o Word: texto, escaneado, OCR (opcional), sección de requisitos, valor y cronograma."""
    ocr_seconds = None
    ocr_page_count = 0
    if name.lower().endswith(".docx"):
        pages, kind = [_docx_plain(content)], "docx"
        starts = []
        text = pages[0]
        scanned = False   # un Word siempre trae texto; si viene casi vacío, se ve en texto_caracteres
        n_pages = 0
    else:
        pages, kind = pdf_pages_text(content), "pdf"
        n_pages = len(pages)
        scanned = (sum(len(p.strip()) for p in pages) / n_pages if n_pages else 0) < sp.SCANNED_CHARS_PER_PAGE
        image_pages = [i + 1 for i, p in enumerate(pages) if len(p.strip()) < sp.SCANNED_CHARS_PER_PAGE]
        if scanned and use_ocr:
            begin = time.perf_counter()
            pages = ocr_pdf(content)
            ocr_seconds = round(time.perf_counter() - begin, 1)
            ocr_page_count = len(pages)
        elif use_ocr and image_pages:
            # PDF con texto que trae páginas escaneadas: solo esas pasan por OCR
            begin = time.perf_counter()
            for number, text_of_page in zip(image_pages, ocr_pdf(content, image_pages)):
                pages[number - 1] = text_of_page
            ocr_seconds = round(time.perf_counter() - begin, 1)
            ocr_page_count = len(image_pages)
        text, starts = join_pages(pages)
    sections = requirement_sections(text)
    best = sections[0] if sections else None
    tokens = round(len(best["text"]) / sp.CHARS_PER_TOKEN) if best else 0
    info = {
        "formato_documento": kind,
        "paginas": n_pages,
        "escaneado": scanned,
        "ocr_usado": ocr_seconds is not None,
        "ocr_segundos": ocr_seconds,
        "texto_caracteres": len(text),
        "tokens_documento_est": round(len(text) / sp.CHARS_PER_TOKEN),
        "tokens_gemini_pdf_est": n_pages * sp.GEMINI_TOKENS_PER_PDF_PAGE,
        "apariciones_requisitos": len(sections),
        "seccion_encontrada": best is not None,
        "seccion_es_titulo": bool(best and best["titulo"]),
        "seccion_pagina": page_of(best["start"], starts) if best else None,
        "tokens_seccion_est": tokens,
        # "corta" = parece una línea del índice: muy corta, o con puntos guía hacia un número de página
        "seccion_corta": best is not None and (tokens < SHORT_SECTION_TOKENS
                                               or bool(INDEX_LEADERS.search(best["text"][:300]))),
        "paginas_con_ocr": ocr_page_count,
    }
    # Útil = es el título de la sección y no parece una línea del índice. Una mención dentro de un
    # párrafo NO es la sección: en las bases del 28/10 esas menciones eran texto del procedimiento.
    info["seccion_util"] = info["seccion_es_titulo"] and not info["seccion_corta"]
    info.update(value_and_schedule(text, starts))
    return info, (best["text"] if best else "")


# ---------------------------------------------------------------------------
# 4. Selección y recorrido de las bases
# ---------------------------------------------------------------------------
def load_sp005():
    docs = pd.read_csv(SP005_DIR / "documentos.csv", dtype={"codigo": str})
    vigentes = {r["ocid"]: r for r in json.loads((SP005_DIR / "vigentes.json").read_text(encoding="utf-8"))}
    return docs, vigentes


def base_path(row):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(row["ocid"]))
    return SP005_DIR / "bases" / f"{safe}.{row['formato_real']}"


def rubros_of(vigente):
    return " ".join(sorted({sp.family_of(i) or "" for i in (vigente or {}).get("items", [])}))


def selected_rows(docs, codes):
    chosen = docs[docs["codigo"].isin(codes)].copy()
    missing = sorted(set(codes) - set(chosen["codigo"]))
    if missing:
        print(f"Aviso: no están en {SP005_DIR / 'documentos.csv'} ({len(docs)} filas): {', '.join(missing)}")
        if len(missing) == len(set(codes)):
            print("  Ninguno de los códigos está: esa carpeta parece de otra ejecución del SP-005. Las bases del "
                  "SP-006 son las de la ejecución del 17/10: copien esa carpeta salidas/sp005/ completa "
                  "(documentos.csv, vigentes.json y bases/).")
    order = {c: i for i, c in enumerate(codes)}
    return chosen.sort_values("codigo", key=lambda s: s.map(order))


def rows_from_sp005(codes):
    """(fila, ruta) de cada base, tomadas de la ejecución del SP-005 (documentos.csv y bases/)."""
    docs, vigentes = load_sp005()
    for _, d in selected_rows(docs, codes).iterrows():
        vigente = vigentes.get(d["ocid"], {})
        yield {"codigo": d["codigo"], "ocid": d["ocid"], "rubros": rubros_of(vigente),
               "metodo": vigente.get("method"), "entidad": d["entidad"], "documento": d["documento"],
               "formato_descarga": d["formato_real"],
               "motivo_seleccion": SUGGESTED_SCANNED.get(d["codigo"]) or SUGGESTED_TEXT.get(d["codigo"], "")}, base_path(d)


def rows_from_files(pairs):
    """(fila, ruta) de bases descargadas a mano: "CODIGO=RUTA". No necesita documentos.csv ni la API.

    El rubro, el método y la entidad quedan vacíos: se completan con el bases_sp006.csv del 28/10.
    """
    for pair in pairs:
        code, _, path = pair.partition("=")
        if not code.strip() or not path.strip():
            raise SystemExit(f"--archivo espera CODIGO=RUTA, por ejemplo 1258127=~/Downloads/bases.zip (recibió {pair!r})")
        path = Path(path.strip()).expanduser()
        kind = sp.file_kind(path.read_bytes()) if path.exists() else path.suffix.lstrip(".").lower()
        code = code.strip()
        yield {"codigo": code, "ocid": "", "rubros": "", "metodo": "", "entidad": "",
               "documento": f"descargado a mano: {path.name}", "formato_descarga": kind,
               "motivo_seleccion": SUGGESTED_SCANNED.get(code) or SUGGESTED_TEXT.get(code, "")}, path


def command_read(args):
    if getattr(args, "archivo", None):
        selected = rows_from_files(args.archivo)
    else:
        codes = args.codigos.split(",") if args.codigos else list(SUGGESTED_SCANNED) + list(SUGGESTED_TEXT)
        selected = rows_from_sp005(codes)
    use_ocr = args.ocr and ocr_available()
    if args.ocr and not use_ocr:
        print("Aviso: --ocr necesita pdftoppm (Poppler) y tesseract con el idioma spa; las escaneadas se miden sin OCR.")
    (OUTPUT_DIR / "secciones").mkdir(parents=True, exist_ok=True)
    rows = []
    for row, path in selected:
        d = row
        try:
            content = path.read_bytes()
            if row["formato_descarga"] == "rar":
                row["version_rar"] = rar_version(content)
            files, tool = documents_in(content, sp.file_kind(content))
            row["abierto_con"] = tool
            if not files:
                raise RuntimeError("no contiene PDF ni Word")
            measured = [(analyze_document(n, b, use_ocr), n) for n, b in files]
            # Si el comprimido trae varios documentos: primero el que tiene la sección como título,
            # luego el de sección más larga, luego el que tiene texto (no escaneado).
            # Un título corto (por ejemplo, la última página de unas especificaciones técnicas) se prefiere a
            # una mención dentro de un párrafo, aunque esta sea más larga: la mención nunca son los requisitos.
            (info, section), name = max(measured, key=lambda m: (m[0][0]["seccion_util"], m[0][0]["seccion_es_titulo"],
                                                                   m[0][0]["tokens_seccion_est"],
                                                                   not m[0][0]["escaneado"], m[0][0]["texto_caracteres"]))
            row.update(info)
            row["archivo_medido"] = name
            row["documentos_en_archivo"] = len(files)
            if section:
                # Las menciones dentro de un párrafo se guardan aparte, para no leerlas como requisitos
                suffix = "" if info["seccion_util"] else ("_corta" if info["seccion_es_titulo"] else "_mencion")
                (OUTPUT_DIR / "secciones" / f"{d['codigo']}{suffix}.txt").write_text(section, encoding="utf-8")
            status = ("escaneada" + (" + OCR" if info["ocr_usado"] else "") if info["escaneado"]
                      else "con texto" + (f" + OCR de {info['paginas_con_ocr']} páginas escaneadas" if info["ocr_usado"] else ""))
            if info["seccion_util"]:
                found = f"sección p. {info['seccion_pagina']} ~{info['tokens_seccion_est']} tokens"
            elif info["seccion_corta"]:
                found = (f"título corto en p. {info['seccion_pagina']} (~{info['tokens_seccion_est']} tokens): "
                         f"revisar si es el índice o la sección ({d['codigo']}_corta.txt)"
                         if info["seccion_es_titulo"] else "solo una línea corta (parece índice)")
            elif info["seccion_encontrada"]:
                found = "solo menciones dentro de párrafos: sección no encontrada como título"
            else:
                found = "sección no encontrada"
            print(f"  {d['codigo']}: {row['formato_descarga']} → {status} · {found}")
        except FileNotFoundError:
            row["error"] = f"No está el archivo {path}"
            print(f"  {d['codigo']}: {row['error']}")
        except Exception as error:  # archivo dañado o herramienta ausente: se registra y se sigue
            row["error"] = f"{type(error).__name__}: {error}"[:300]
            print(f"  {d['codigo']}: {row['error']}")
        rows.append(row)
    if not rows:
        print("\nBases leídas: 0 de 0. No se escribió nada.")
        return
    table = pd.DataFrame(rows)
    save_merged(table)
    write_catalog_template()
    print_summary(table)


def save_merged(table):
    """Guarda bases_sp006.csv sin perder las bases leídas antes: las mismas se reemplazan, las demás quedan."""
    path = OUTPUT_DIR / "bases_sp006.csv"
    if path.exists():
        try:
            previous = pd.read_csv(path, dtype={"codigo": str})
        except pd.errors.EmptyDataError:   # un archivo vacío de una ejecución anterior sin bases
            previous = pd.DataFrame(columns=["codigo"])
        if "codigo" in previous:
            previous = previous[~previous["codigo"].isin(table["codigo"])]
            table = pd.concat([previous, table], ignore_index=True)
    table.to_csv(path, index=False, encoding="utf-8-sig")


def write_catalog_template():
    path = OUTPUT_DIR / "catalogo_requisitos.csv"
    if path.exists():  # nunca se pisa el catálogo que el equipo ya empezó a llenar
        return
    template = pd.DataFrame([{c: "" for c in CATALOG_COLUMNS} | {"tipo_requisito": t,
                              "observaciones": "candidato: confirmar o borrar al leer las bases"}
                             for t in CANDIDATE_TYPES], columns=CATALOG_COLUMNS)
    template.to_csv(path, index=False, encoding="utf-8-sig")


def print_summary(table):
    ok = (table[table["error"].isna()] if "error" in table else table).copy()
    print(f"\nBases leídas: {len(ok)} de {len(table)}")
    if not len(ok):
        return
    for column in ("escaneado", "ocr_usado", "seccion_encontrada", "seccion_corta", "seccion_util",
                   "valor_con_monto", "presentacion_ofertas_con_fecha"):
        ok[column] = ok[column].fillna(False).astype(bool)
    text = ok[~ok["escaneado"]]
    scanned = ok[ok["escaneado"]]
    print(f"Con texto: {len(text)} · escaneadas: {len(scanned)} (con OCR: {int(ok['ocr_usado'].sum())})")
    found = ok[ok["seccion_util"]]
    mentions = ok[ok["seccion_encontrada"] & ~ok["seccion_util"] & ~ok["seccion_corta"]]
    print(f"Sección de requisitos útil: {len(found)} · solo menciones en párrafos: {len(mentions)} · "
          f"corta (parece índice): {int(ok['seccion_corta'].sum())} · "
          f"no encontrada: {int((~ok['seccion_encontrada']).sum())}")
    if len(found):
        print(f"Tokens de la sección (mediana): {int(found['tokens_seccion_est'].median()):,}")
    print(f"Valor estimado con monto en el texto: {int(ok['valor_con_monto'].sum())} de {len(ok)} · "
          f"presentación de ofertas con fecha: {int(ok['presentacion_ofertas_con_fecha'].sum())} de {len(ok)}")
    print(f"Detalle: {OUTPUT_DIR / 'bases_sp006.csv'} · secciones en {OUTPUT_DIR / 'secciones'}")


# ---------------------------------------------------------------------------
# 5. Prueba de RAR (actividad 6)
# ---------------------------------------------------------------------------
def command_rar_check(_args):
    """Prueba cada RAR de salidas/sp005/bases/ con cada herramienta disponible."""
    tools = extractors()
    print("Herramientas encontradas: " + (", ".join(n for n, _ in tools) or "ninguna"))
    rows = []
    for path in sorted((SP005_DIR / "bases").glob("*.rar")):
        content = path.read_bytes()
        for name, cmd in tools:
            try:
                code, files = extract_with(cmd, content, "rar")
                readable = 0
                for fname, data in files:
                    try:
                        if fname.lower().endswith(".pdf"):
                            PdfReader(io.BytesIO(data))
                        readable += 1
                    except Exception:
                        pass
                rows.append({"archivo": path.name, "version_rar": rar_version(content), "herramienta": name,
                             "codigo_salida": code, "documentos": len(files), "documentos_legibles": readable,
                             "funciona": readable > 0})
            except Exception as error:
                rows.append({"archivo": path.name, "version_rar": rar_version(content), "herramienta": name,
                             "error": f"{type(error).__name__}: {error}"[:200], "funciona": False})
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(rows)
    table.to_csv(OUTPUT_DIR / "rar_linux.csv", index=False, encoding="utf-8-sig")
    print(table.to_string(index=False) if len(table) else "No hay archivos .rar en salidas/sp005/bases/")


def command_diagnose(args):
    """Muestra cada línea con "califica" de una base, con su página y si cuenta como título."""
    docs, _ = load_sp005()
    out = []
    for _, d in selected_rows(docs, args.diagnostico.split(",")).iterrows():
        content = base_path(d).read_bytes()
        files, tool = documents_in(content, sp.file_kind(content))
        for name, data in files:
            pages = [_docx_plain(data)] if name.lower().endswith(".docx") else pdf_pages_text(data)
            text, starts = join_pages(pages)
            out.append(f"== {d['codigo']} · {name} · {len(pages)} páginas · {len(text):,} caracteres")
            for match in QUALIFY_WORD.finditer(text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                line = text[line_start: line_end if line_end > 0 else None].strip()
                req = REQUIREMENTS_PATTERN.search(text, line_start, line_end if line_end > 0 else len(text))
                mark = "TÍTULO " if req and is_heading(text, req.start()) else "       "
                out.append(f"  p. {page_of(match.start(), starts) or '-':>3} {mark}{line[:140]}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"diagnostico_{args.diagnostico.replace(',', '_')}.txt"
    path.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out[:120]))
    print(f"\nCompleto en {path}")


def main():
    sp.safe_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codigos", help="Códigos de convocatoria separados por comas (por defecto, los 13 sugeridos).")
    parser.add_argument("--ocr", action="store_true", help="Leer las escaneadas con OCR (pdftoppm + tesseract spa).")
    parser.add_argument("--archivo", action="append", metavar="CODIGO=RUTA",
                        help="Leer una base descargada a mano, sin documentos.csv ni la API. Se puede repetir.")
    parser.add_argument("--rar-check", action="store_true", help="Solo probar la extracción de los RAR.")
    parser.add_argument("--diagnostico", metavar="CODIGOS",
                        help="Listar las líneas con 'califica' de esas bases, para ver por qué no se halló la sección.")
    args = parser.parse_args()
    if args.diagnostico:
        command_diagnose(args)
    elif args.rar_check:
        command_rar_check(args)
    else:
        command_read(args)


if __name__ == "__main__":
    main()