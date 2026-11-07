
import re
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import select

from services import db

MIN_OVERLAP = 20          # menos que esto no se considera solape (podría ser una coincidencia)
MAX_OVERLAP = 600         # el solape real es de ~200 caracteres (rag.CHUNK_OVERLAP)

# Encabezados dentro de la sección: "A. CAPACIDAD LEGAL", "B.1 EXPERIENCIA", "3.2. REQUISITOS…", "C) …"
HEADING = re.compile(r"^\s*(?:[A-H](?:\.\d{1,2})*[\.\)]|\d{1,2}(?:\.\d{1,2}){0,3}\.?\)?)\s+\S")

# Pie de página del documento ("Página 9 de 11", "Pág. 3/20"): no es parte de los requisitos
PAGE_FOOTER = re.compile(r"^\s*p[aá]g(?:ina|\.)?\s*\d+\s*(?:de|/)\s*\d+\s*$", re.IGNORECASE)
PAGE_NUMBER = re.compile(r"^\s*\d{1,3}\s*$")     # número de página impreso, solo en su línea ("37")
HEADER_MIN_CHARS = 30     # un encabezado de página (nombre de la entidad, del proceso) que se repite
# Títulos sin numeración (frecuentes en las bases en Word): "EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD"
TITLE_WORDS = re.compile(r"\b(REQUISITOS?|CAPACIDAD|EXPERIENCIA|HABILITACI[OÓ]N(ES)?|EQUIPAMIENTO|PERSONAL|"
                         r"INFRAESTRUCTURA|CONSORCIO|SOLVENCIA|CALIFICACI[OÓ]N)\b")

# Mensajes de la ficha cuando no hay requisitos para mostrar
UNAVAILABLE = {
    "pendiente": ("Las bases de esta convocatoria aún no se han leído.",
                  "Se leen en la próxima actualización diaria. Mientras tanto, puede revisar los requisitos en las bases."),
    "seccion_no_encontrada": ("Leímos las bases, pero no encontramos la sección de requisitos de calificación.",
                              "Puede tener otro nombre o venir en un documento aparte. Revísela en las bases."),
    "no_legible": ("No pudimos leer las bases de esta convocatoria.",
                   "El archivo no tiene texto que se pueda extraer o viene en un formato que no se abre."),
    "error": ("No pudimos descargar o leer las bases en la última actualización.",
              "Se volverá a intentar en la próxima actualización diaria."),
}


@dataclass
class Block:
    """Texto de los requisitos que empieza en una página de las bases."""
    page: Optional[int]                                     # None: documento Word (no tiene páginas)
    text: str = ""
    segments: List[tuple] = field(default_factory=list)     # [("titulo" | "texto", texto)]


@dataclass
class Requirements:
    status: str                      # requirements_status de la convocatoria
    available: bool                  # hay requisitos para mostrar
    blocks: List[Block] = field(default_factory=list)
    pages: List[int] = field(default_factory=list)          # páginas de las bases donde están
    source: Optional[str] = None     # "texto" u "ocr"
    file_name: Optional[str] = None  # documento de las bases de donde salieron
    title: Optional[str] = None      # mensaje cuando no hay requisitos
    detail: Optional[str] = None


def overlap(previous, current):
    """Cuántos caracteres del inicio de `current` repiten el final de `previous` (0 si no se solapan)."""
    longest = min(len(previous), len(current), MAX_OVERLAP)
    for size in range(longest, MIN_OVERLAP - 1, -1):
        if previous.endswith(current[:size]):
            return size
    return 0


def _mostly_upper(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.6


def _header_key(line):
    """Solo las letras, sin tildes: el OCR lee el mismo encabezado con pequeñas diferencias en cada página
    ("N.?" en una, "N.°" en otra; ejecución en Windows del 07/11/2026, base 1263868)."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", line.upper()).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z]", "", plain)


def _only_page_header(text):
    """Lo que queda de una página que solo aporta su encabezado (la sección terminó en la anterior)."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return (0 < len(lines) <= 4 and all(_mostly_upper(line) for line in lines)
            and not any(HEADING.match(line) or TITLE_WORDS.search(line) for line in lines))


def _clean(blocks):
    """Quita los pies de página y los encabezados que el documento repite en cada página."""
    from collections import Counter
    keys = Counter(_header_key(line) for b in blocks for line in b.text.split("\n")
                   if len(line.strip()) >= HEADER_MIN_CHARS and _mostly_upper(line) and not HEADING.match(line.strip()))
    repeated = {key for key, n in keys.items() if n >= 2 and len(key) >= 20}
    for block in blocks:
        kept = [line for line in block.text.split("\n")
                if not PAGE_FOOTER.match(line) and not PAGE_NUMBER.match(line)
                and not (len(line.strip()) >= HEADER_MIN_CHARS and _header_key(line) in repeated)]
        block.text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    blocks = [b for b in blocks if b.text]
    if len(blocks) > 1 and _only_page_header(blocks[-1].text):
        blocks = blocks[:-1]          # el encabezado de la página donde empieza el capítulo siguiente
    return blocks


def _segments(text):
    """Separa los encabezados ("B. EXPERIENCIA DEL POSTOR") del texto, para resaltarlos en la ficha."""
    result, buffer = [], []
    for line in text.split("\n"):
        stripped = line.strip()
        letters = [c for c in stripped if c.isalpha()]
        numbered = HEADING.match(stripped) and len(stripped) <= 120 and _mostly_upper(stripped)
        plain = (8 <= len(stripped) <= 90 and not stripped.endswith((".", ":", ";", ","))
                 and _mostly_upper(stripped) and sum(c.isupper() for c in stripped) >= 0.85 * sum(c.isalpha() for c in stripped)
                 and TITLE_WORDS.search(stripped))
        is_heading = numbered or plain
        if is_heading:
            if buffer:
                result.append(("texto", "\n".join(buffer).strip()))
                buffer = []
            result.append(("titulo", stripped))
        elif stripped or buffer:
            buffer.append(stripped)
    if buffer and "\n".join(buffer).strip():
        result.append(("texto", "\n".join(buffer).strip()))
    return result


def merge_chunks(chunks):
    """[(página, texto)] en orden → [Block] por página, sin repetir el solape entre fragmentos."""
    blocks, previous = [], ""
    for page, text in chunks:
        text = (text or "").strip()
        cut = overlap(previous, text) if previous else 0
        new = text[cut:].strip()
        previous = text
        if not new:
            continue
        if blocks and blocks[-1].page == page:
            blocks[-1].text += "\n" + new
        else:
            blocks.append(Block(page=page, text=new))
    blocks = _clean(blocks)
    for block in blocks:                 # el texto de cada página, ya unido, se separa en títulos y párrafos
        block.segments = _segments(block.text)
    return blocks


def for_tender(session, tender_id, status):
    """Requisitos de una convocatoria a partir de sus fragmentos guardados (la base vectorial)."""
    chunks = list(session.execute(
        select(db.RequirementChunk.page, db.RequirementChunk.text, db.RequirementChunk.source,
               db.RequirementChunk.file_name)
        .where(db.RequirementChunk.tender_id == tender_id).order_by(db.RequirementChunk.chunk_index)))
    status = status or "pendiente"
    if status == "extraido" and chunks:
        blocks = merge_chunks([(c.page, c.text) for c in chunks])
        pages = sorted({b.page for b in blocks if b.page})
        return Requirements(status=status, available=True, blocks=blocks, pages=pages,
                            source=chunks[0].source, file_name=chunks[0].file_name)
    if status == "extraido":           # marcada como leída pero sin fragmentos: se trata como no legible
        status = "no_legible"
    title, detail = UNAVAILABLE.get(status, UNAVAILABLE["pendiente"])
    return Requirements(status=status, available=False, title=title, detail=detail)