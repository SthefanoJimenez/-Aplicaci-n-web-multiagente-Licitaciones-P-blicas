"""HU-018: etapa de una convocatoria y su cronograma.

Etapa. Sale de los documentos que publica la API (los que ya guarda la ingesta). En 10 convocatorias
vigentes consultadas el 13/11/2026 (`python ingest_job.py --etapas 10`) la API publicó, con su fecha
(datePublished): «Bases Administrativas» y «Bases Integradas» (biddingDocuments), «Pliego de absolución
de consultas y observaciones» o «Acta de no formulación de consultas y observaciones» (clarifications)
y «Documentos de Otorgamiento de Buena Pro» (awardNotice). De ahí las etapas:
- «Buena pro otorgada»: hay un documento de otorgamiento de la buena pro.
- «Bases integradas»: la entidad ya integró las bases; no cambian más.
- «Absolución de consultas y observaciones»: se publicó el pliego o el acta de no formulación.
- «Bases administrativas»: solo están las bases de la convocatoria.

Cronograma, de dos fuentes, sin inventar fechas:
1. La API: el plazo de consultas y observaciones (tender.enquiryPeriod, en 6 de esas 10) y la fecha en
   que se publicó cada documento (pliego, bases integradas, buena pro). tenderPeriod no sirve: empieza y
   termina el día de la convocatoria (SP-005).
2. Las bases: la sección «Cronograma del procedimiento de selección», tomando solo las etapas que tienen
   una fecha escrita en ella. En las 6 bases reales revisadas el 13/11/2026 (1255879, 1257079, 1258549,
   1262142, 1262520 y 1263973) esa sección no trae fechas: dice «Según el cronograma de la ficha de
   selección de la convocatoria publicada en el SEACE». Ese caso queda como "remite_seace" y la ficha lo
   dice con la página.
"""
from dataclasses import dataclass, field
from datetime import date
import re
import unicodedata

from services.ocds_source import normalize

# --------------------------------------------------------------------------- etapas
ADMINISTRATIVAS, ABSOLUCION, INTEGRADAS, BUENA_PRO = "administrativas", "absolucion", "integradas", "buena_pro"
STAGES = [
    (ADMINISTRATIVAS, "Bases administrativas",
     "Es la etapa de consultas y observaciones: las bases todavía pueden cambiar."),
    (ABSOLUCION, "Absolución de consultas y observaciones",
     "La entidad publicó el pliego de absolución: las bases cambiarán con lo absuelto al integrarse."),
    (INTEGRADAS, "Bases integradas",
     "Las bases ya no cambian: son las reglas definitivas para presentar la oferta."),
    (BUENA_PRO, "Buena pro otorgada",
     "La entidad ya publicó el otorgamiento de la buena pro: el plazo para presentar ofertas terminó."),
]
STAGE_NAMES = {key: name for key, name, _ in STAGES}
STAGE_HELP = {key: text for key, _, text in STAGES}
STAGE_ORDER = {key: i for i, (key, _, _) in enumerate(STAGES)}
NO_QUERIES_HELP = ("Nadie formuló consultas ni observaciones (acta de no formulación): las bases se integran "
                   "sin cambios.")

PLIEGO = re.compile(r"\bpliego\b|absolucion\w*\s+de\s+(las\s+)?(consultas|observaciones)")
NO_QUERIES = re.compile(r"\bno\s+formulacion\s+de\s+consultas")
AWARD = re.compile(r"buena\s+pro")
ANY_BASES = re.compile(r"\bbases?\b")
CLARIFICATIONS, AWARD_NOTICE = "clarifications", "awardnotice"


def _title_and_type(doc):
    title = doc.get("title") if isinstance(doc, dict) else getattr(doc, "title", None)
    kind = doc.get("type") if isinstance(doc, dict) else getattr(doc, "doc_type", None)
    return normalize(title or ""), normalize(kind or "")


def stage_of(documents):
    """Etapa según los documentos publicados: [{"title", "type"}] o filas con .title y .doc_type.

    Devuelve "buena_pro", "integradas", "absolucion", "administrativas" o None si no hay bases publicadas.
    """
    docs = [_title_and_type(d) for d in documents or []]
    if any(kind == AWARD_NOTICE or AWARD.search(title) for title, kind in docs):
        return BUENA_PRO
    if any("bases integradas" in title for title, _ in docs):
        return INTEGRADAS
    if any(PLIEGO.search(title) or NO_QUERIES.search(title) or kind == CLARIFICATIONS for title, kind in docs):
        return ABSOLUCION
    if any(ANY_BASES.search(title) or kind == "biddingdocuments" for title, kind in docs):
        return ADMINISTRATIVAS
    return None


def is_stage_document(doc):
    """Documento que marca una etapa (bases, pliego o acta, buena pro): los que la ficha lista con su fecha."""
    title, kind = _title_and_type(doc)
    return bool(ANY_BASES.search(title) or PLIEGO.search(title) or NO_QUERIES.search(title) or AWARD.search(title)
                or kind in (CLARIFICATIONS, AWARD_NOTICE))


def stage_steps(stage):
    """[(nombre, "hecha" | "actual" | "pendiente")] para dibujar las etapas en la ficha."""
    current = STAGE_ORDER.get(stage)
    steps = []
    for key, name, _ in STAGES:
        order = STAGE_ORDER[key]
        state = "pendiente" if current is None or order > current else ("actual" if order == current else "hecha")
        steps.append((name, state))
    return steps


# --------------------------------------------------------------------------- cronograma
# Estados de la lectura (columna convocatorias.schedule_status; vacío = nunca se leyó). Si hay que
# leerlo otra vez (cambió la etapa), schedule_due queda en True y se conserva la lectura anterior.
LEIDO, REMITE_SEACE, NO_ENCONTRADO, NO_LEGIBLE = "leido", "remite_seace", "no_encontrado", "no_legible"
SCHEDULE_STATUSES = (LEIDO, REMITE_SEACE, NO_ENCONTRADO, NO_LEGIBLE)
STATUS_RANK = {NO_LEGIBLE: 0, NO_ENCONTRADO: 1, REMITE_SEACE: 2, LEIDO: 3}

# Etapas del cronograma, en el orden en que se prueban: «Absolución de consultas y observaciones»
# también dice «consultas y observaciones», así que la absolución va antes que las consultas.
EVENTS = [
    ("absolucion", "Absolución de consultas y observaciones", r"absolucion"),
    ("integracion", "Integración de las bases",
     r"integracion\s+de\s+(las\s+)?bases|publicacion\s+de\s+(las\s+)?bases\s+integradas"),
    ("ofertas", "Presentación de ofertas", r"presentacion\s+de\s+(las\s+)?ofertas"),
    ("evaluacion", "Evaluación y calificación", r"evaluacion\s+y\s+calificacion|calificacion\s+y\s+evaluacion"),
    ("buena_pro", "Otorgamiento de la buena pro", r"buena\s+pro"),
    ("registro", "Registro de participantes", r"registro\s+de\s+participantes"),
    ("consultas", "Formulación de consultas y observaciones",
     r"formulacion\s+de\s+consultas|consultas\s+y\s+(/\s*u\s+)?observaciones|consultas\s+u\s+observaciones"),
    ("convocatoria", "Convocatoria", r"^\W*convocatoria\b"),
]
EVENT_PATTERNS = [(key, name, re.compile(pattern)) for key, name, pattern in EVENTS]
EVENT_ORDER = ["convocatoria", "registro", "consultas", "absolucion", "integracion", "ofertas", "evaluacion",
               "buena_pro"]

# El título ocupa la línea entera («2.1 CRONOGRAMA DEL PROCEDIMIENTO DE SELECCIÓN»): así no se confunde con
# frases de la sección general como «…en la fecha prevista en el cronograma del procedimiento de selección».
HEADING = re.compile(r"(?:[0-9]+(?:\.[0-9]+)*\.?|[ivx]+\.|[a-z]\.)?\s*(cronograma|calendario)\s+del\s+"
                     r"(procedimiento|proceso)(\s+de\s+seleccion)?")
NEXT_HEADING = re.compile(r"^[\s|]*(?:[0-9]+\.[0-9]+(?:\.[0-9]+)*\.?\s+[a-z]{3,}|capitulo\b|seccion\b|anexo\b)")
REFERRAL = re.compile(r"ficha\s+de\s+(la\s+)?seleccion|publicad[oa]\s+en\s+el\s+seace|segun\s+el\s+cronograma")
MAX_SECTION_LINES = 60
MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
          "setiembre": 9, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)")
TEXT_DATE = re.compile(r"(?<!\d)(\d{1,2})\s+de\s+(" + "|".join(MONTHS) + r")\s+(?:de|del)\s+(\d{4})")


@dataclass
class ScheduleResult:
    status: str                           # LEIDO, REMITE_SEACE, NO_ENCONTRADO o NO_LEGIBLE
    events: list = field(default_factory=list)   # [{"key","name","start","end","page","text"}], fechas ISO
    page: int | None = None               # página donde está la sección del cronograma
    document: str | None = None           # archivo dentro de lo descargado
    detail: str = ""


def _plain(text):
    """Minúsculas sin tildes, conservando los separadores de las fechas (normalize quita los guiones)."""
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()


def dates_in(text):
    """Fechas válidas escritas en el texto, en orden: «20/11/2026», «20-11-2026» o «20 de noviembre de 2026»."""
    text = re.sub(r"\s+", " ", _plain(text))
    found = []
    for match in NUMERIC_DATE.finditer(text):
        found.append((match.start(), int(match.group(3)), int(match.group(2)), int(match.group(1))))
    for match in TEXT_DATE.finditer(text):
        found.append((match.start(), int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))))
    dates = []
    for _pos, year, month, day in sorted(found):
        try:
            dates.append(date(year, month, day))
        except ValueError:
            continue
    return dates


def _event_of(line):
    clean = normalize(line)
    for key, name, pattern in EVENT_PATTERNS:
        if pattern.search(clean):
            return key, name
    return None


def _lines_with_positions(text):
    position = 0
    for line in text.split("\n"):
        yield position, line
        position += len(line) + 1


def find_schedule(text, page_of=None, document=None):
    """Cronograma escrito en el texto de un documento de las bases. page_of(posición) da la página.

    Solo toma una fecha si está en la línea de su etapa o en las dos siguientes (las tablas a veces
    separan la etapa y la fecha en líneas distintas), dentro de la sección del cronograma.
    """
    page_of = page_of or (lambda position: None)
    lines = list(_lines_with_positions(text or ""))
    start = next((i for i, (_pos, line) in enumerate(lines) if HEADING.fullmatch(normalize(line))), None)
    if start is None:
        return ScheduleResult(NO_ENCONTRADO, document=document, detail="las bases no tienen la sección del cronograma")
    page = page_of(lines[start][0])
    section = []
    for position, line in lines[start + 1:start + 1 + MAX_SECTION_LINES]:
        if NEXT_HEADING.search(normalize(line)) and section and any(l.strip() for _p, l in section):
            break
        section.append((position, line))
    events, used = {}, set()
    for i, (position, line) in enumerate(section):
        event = _event_of(line)
        if event is None or event[0] in events:
            continue
        found, evidence = dates_in(line), [line.strip()]
        for j in range(i + 1, min(i + 3, len(section))):
            if found or j in used or _event_of(section[j][1]):
                break
            found = dates_in(section[j][1])
            if found:
                used.add(j)
                evidence.append(section[j][1].strip())
        if found:
            key, name = event
            events[key] = {"key": key, "name": name, "start": found[0].isoformat(),
                           "end": found[1].isoformat() if len(found) > 1 and found[1] != found[0] else None,
                           "page": page_of(position), "text": " ".join(" ".join(evidence).split())[:300]}
    if events:
        ordered = [events[k] for k in EVENT_ORDER if k in events]
        return ScheduleResult(LEIDO, events=ordered, page=page, document=document,
                              detail=f"{len(ordered)} etapa(s) con fecha")
    joined = normalize(" ".join(line for _p, line in section[:12]))
    if REFERRAL.search(joined):
        return ScheduleResult(REMITE_SEACE, page=page, document=document,
                              detail="las bases remiten al cronograma de la ficha de selección del SEACE")
    return ScheduleResult(NO_ENCONTRADO, page=page, document=document,
                          detail="la sección del cronograma no trae fechas reconocibles")


def better(current, new):
    """La mejor de dos lecturas (de documentos distintos dentro de lo descargado)."""
    if current is None:
        return new
    if new is None:
        return current
    return new if STATUS_RANK[new.status] > STATUS_RANK[current.status] else current


def schedule_of_document(doc):
    """find_schedule sobre un bases_reader.DocumentText (con sus páginas, si es PDF)."""
    return find_schedule(doc.text, doc.page_of if doc.starts else None, doc.name)


# --------------------------------------------------------------------------- guardar
def mark_stage(tender, documents, now):
    """Actualiza la etapa con los documentos publicados. Si cambió (llegó el pliego o las bases
    integradas) o el cronograma nunca se leyó, lo deja para leer en la siguiente ingesta (schedule_due)."""
    stage = stage_of(documents)
    changed = stage != tender.stage
    if changed:
        tender.stage = stage
        tender.stage_changed_at = now
    if stage is not None and (changed or tender.schedule_status is None):
        tender.schedule_due = True
    return changed


def save(tender, result, document, now):
    """Guarda la lectura del cronograma en la convocatoria y la marca como leída.

    Una lectura peor del MISMO documento (por ejemplo, en una computadora sin OCR) no borra una buena;
    si las bases cambiaron (llegaron las integradas), la nueva lectura reemplaza a la anterior.
    """
    tender.schedule_due = False
    tender.schedule_read_at = now
    tender.schedule_stage = tender.stage
    same_document = document is not None and tender.schedule_document_id == document.id
    if (same_document and tender.schedule_status in STATUS_RANK
            and STATUS_RANK[result.status] < STATUS_RANK[tender.schedule_status]):
        return False                          # se revisó; se conserva lo que ya se había leído
    tender.schedule_status = result.status
    tender.schedule = result.events or None
    tender.schedule_page = result.page
    # Título de las bases al leerlas + archivo dentro de lo descargado: «Bases Integradas · bases.pdf»
    source = " · ".join(x for x in (getattr(document, "title", None), result.document) if x)
    tender.schedule_file = source[:300] or None
    tender.schedule_document_id = document.id if document is not None else None
    return True


# --------------------------------------------------------------------------- ficha
SEACE_SEARCH_URL = "https://prodapp2.seace.gob.pe/seacebus-uiwd-pub/buscadorConvocatorias/buscadorConvocatorias.xhtml"
SHORT_MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "set", "oct", "nov", "dic"]


def day_text(value):
    """«2026-11-20» o un datetime -> «20 nov 2026» (en hora de Lima si trae hora)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    elif hasattr(value, "astimezone") and getattr(value, "tzinfo", None) is not None:
        from services.ocds_source import LIMA
        value = value.astimezone(LIMA)
    return f"{value.day} {SHORT_MONTHS[value.month - 1]} {value.year}"


def moment_text(value):
    """datetime -> «9 nov 2026, 20:44» en hora de Lima."""
    if value is None:
        return None
    from services.ocds_source import LIMA
    local = value.astimezone(LIMA) if getattr(value, "tzinfo", None) is not None else value
    return f"{day_text(local)}, {local:%H:%M}"


def view(tender):
    """Lo que muestra la ficha (tender.html) a partir de una convocatoria del contrato (tender_contract.Tender)."""
    events = []
    for event in tender.schedule or []:
        events.append({"name": event.get("name"), "start": day_text(event.get("start")),
                       "end": day_text(event.get("end")), "page": event.get("page"), "text": event.get("text")})
    marked = sorted((d for d in tender.documents if d.title and is_stage_document(d)),
                    key=lambda d: (d.published_at is None, d.published_at or 0))
    stage_documents = [{"title": d.title, "published": moment_text(d.published_at)} for d in marked]
    titles = [normalize(d.title) for d in tender.documents if d.title]
    stage_help = STAGE_HELP.get(tender.stage)
    if tender.stage == ABSOLUCION and any(NO_QUERIES.search(t) for t in titles) and not any(PLIEGO.search(t) for t in titles):
        stage_help = NO_QUERIES_HELP
    enquiry = None
    if tender.enquiry_start and tender.enquiry_end:
        enquiry = {"start": moment_text(tender.enquiry_start), "end": moment_text(tender.enquiry_end)}
    reread = bool(tender.schedule_due and tender.schedule_read_at)
    return {
        "stage": tender.stage,
        "stage_name": STAGE_NAMES.get(tender.stage),
        "stage_help": stage_help,
        "stage_changed": day_text(tender.stage_changed_at),
        "steps": stage_steps(tender.stage),
        "stage_documents": stage_documents,
        "enquiry": enquiry,
        "status": tender.schedule_status if tender.schedule_read_at else None,
        "events": events,
        "page": tender.schedule_page,
        "source": tender.schedule_file,
        "read_at": day_text(tender.schedule_read_at),
        "read_stage": STAGE_NAMES.get(tender.schedule_stage),
        "reread_pending": reread,
        "seace_search_url": SEACE_SEARCH_URL,
    }