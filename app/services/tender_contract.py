"""Contrato de datos de las convocatorias vigentes (EN-007).

Define qué guarda la base y qué reciben las pantallas (HU-007) y la extracción de requisitos
(HU-009): sus entidades, sus campos, el tipo de cada uno y si puede faltar. Las tablas de
services/db.py siguen este contrato campo por campo, y tender_contract_example.json trae un
ejemplo completo para trabajar sin base de datos.

Reglas de la fuente que explican los campos opcionales (SP-005 y SP-006):
- El valor estimado casi nunca se publica (1 de 209 vigentes de los pilotos el 17/10) y las
  bases tampoco lo traen: si falta, la pantalla dice "no publicado".
- La fecha límite de ofertas no viene en la API (tenderPeriod dura un día o falta): vigente se
  decide por el estado CONVOCADO de los ítems.
- El departamento es el de la entidad compradora, no el lugar de entrega.

No hay ninguna entidad de usuarios ni de perfiles: el costo y el perfil de la empresa nunca se
guardan (RN-001).
"""
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Optional

EXAMPLE_PATH = Path(__file__).with_name("tender_contract_example.json")

# Valores permitidos en los campos de estado
TENDER_STATES = ("vigente", "no_vigente")
REQUIREMENTS_STATUSES = ("pendiente", "extraido", "seccion_no_encontrada", "no_legible", "error")
RUN_STATUSES = ("en_curso", "ok", "fallida")
REQUIREMENT_SOURCES = ("texto", "ocr")


@dataclass
class TenderDocument:
    """Documento publicado de una convocatoria (bases, anexos…). Tabla "documentos"."""
    title: Optional[str]                 # título en la fuente, por ejemplo "Bases Administrativas"
    url: str                             # enlace de descarga (SEACE)
    doc_type: Optional[str] = None       # documentType de OCDS, por ejemplo "biddingDocuments"
    doc_format: Optional[str] = None     # formato declarado por la fuente (puede no coincidir con el real)
    is_bases: bool = False               # True en el documento que leerá HU-009 (integradas > administrativas)
    published_at: Optional[datetime] = None      # datePublished de OCDS, si la fuente lo trae (HU-018)


@dataclass
class Requirement:
    """Requisito de calificación extraído de las bases (lo llena HU-009). Tabla "requisitos"."""
    requirement_type: str                # una fila de catalogo_requisitos.csv del SP-006
    text: str                            # copia breve y literal del requisito
    page: Optional[int] = None           # página del documento, si se puede saber
    source: str = "texto"                # "texto" (PDF con texto o Word) u "ocr" (escaneado)
    document_url: Optional[str] = None   # documento del que salió


@dataclass
class RequirementChunk:
    """Fragmento de la sección de requisitos de las bases con su vector (RAG, EN-012). Tabla "fragmentos".

    Lo escribe la ingesta (services/rag.py) y lo consultan HU-009 y el asistente. El vector no se
    expone a las pantallas: solo el texto, la página y la convocatoria de la que salió.
    """
    chunk_index: int                     # orden dentro de la sección
    text: str                            # texto del fragmento (unos 1200 caracteres)
    page: Optional[int] = None           # página de las bases donde empieza; None en Word
    source: str = "texto"                # "texto" u "ocr"
    file_name: Optional[str] = None      # archivo dentro de lo descargado (los ZIP y RAR traen varios)
    model: Optional[str] = None          # modelo de embeddings, por ejemplo "gemini-embedding-001"
    tokens_est: Optional[int] = None     # tokens estimados (caracteres / 4), para medir el costo


@dataclass
class Tender:
    """Convocatoria de bienes de los rubros piloto. Tabla "convocatorias"."""
    ocid: str                            # identificador OCDS, clave única (ocds-dgv273-seacev3-…)
    code: Optional[str]                  # tender.id de la fuente (por ejemplo 1258029)
    title: Optional[str]
    entity: Optional[str]                # entidad compradora
    method: Optional[str]                # tipo de procedimiento (procurementMethodDetails)
    rubros: list = field(default_factory=list)   # familias UNSPSC de sus ítems, por ejemplo ["1510"]
    state: str = "vigente"               # "vigente" o "no_vigente"; nunca se borra
    description: Optional[str] = None
    department: Optional[str] = None     # departamento de la entidad (no el de entrega)
    published_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None       # casi siempre falta en la fuente
    estimated_value: Optional[float] = None      # casi siempre falta: "no publicado"
    currency: Optional[str] = None
    item_status: Optional[str] = None    # estados de los ítems en la fuente, por ejemplo "CONVOCADO"
    first_seen_at: Optional[datetime] = None     # cuándo la vio el sistema por primera vez (OE8)
    last_seen_at: Optional[datetime] = None      # última ejecución en que la fuente la traía vigente
    closed_at: Optional[datetime] = None         # cuándo pasó a no_vigente
    requirements_status: str = "pendiente"       # lo actualiza HU-009
    # HU-018 (services/schedule.py): etapa según los documentos y cronograma leído de las bases
    stage: Optional[str] = None                  # "administrativas", "absolucion", "integradas", "buena_pro" o vacío
    stage_changed_at: Optional[datetime] = None
    schedule_status: Optional[str] = None        # "leido", "remite_seace", "no_encontrado", "no_legible"; vacío = sin leer
    schedule: list = field(default_factory=list) # [{"key","name","start","end","page","text"}]: solo fechas escritas en las bases
    schedule_page: Optional[int] = None          # página de la sección del cronograma
    schedule_file: Optional[str] = None          # archivo de lo descargado donde está
    schedule_read_at: Optional[datetime] = None  # «Cronograma actualizado al…»
    schedule_stage: Optional[str] = None         # etapa cuando se leyó
    schedule_due: Optional[bool] = None          # True: cambió la etapa y se volverá a leer en la siguiente ingesta
    enquiry_start: Optional[datetime] = None     # plazo de consultas y observaciones según la API (enquiryPeriod);
    enquiry_end: Optional[datetime] = None       # en las Subastas Inversas no viene
    documents: list = field(default_factory=list)       # [TenderDocument]
    requirements: list = field(default_factory=list)    # [Requirement]


@dataclass
class IngestionRun:
    """Una ejecución de la ingesta diaria. Tabla "actualizaciones"."""
    started_at: datetime
    status: str = "en_curso"             # "en_curso", "ok" o "fallida"
    finished_at: Optional[datetime] = None
    trigger: Optional[str] = None        # "schedule", "workflow_dispatch" (manual) o "local"
    months: Optional[str] = None         # archivos leídos, por ejemplo "2026-10,2026-09"
    source_open_goods: Optional[int] = None     # bienes vigentes en la fuente (todo el país)
    source_open_pilot: Optional[int] = None     # vigentes de los rubros piloto en la fuente (OE8)
    saved_open: Optional[int] = None            # vigentes de los pilotos guardadas tras la ejecución (OE8)
    new_count: Optional[int] = None
    updated_count: Optional[int] = None
    closed_count: Optional[int] = None
    error: Optional[str] = None
    rubros: Optional[str] = None                # rubros descargados: piloto + solicitados, "1510,2510,3011" (EN-011)
    per_rubro: Optional[dict] = None            # vigentes guardadas por rubro tras la ejecución, {"1510": 92} (EN-011)


@dataclass
class RubroRequest:
    """Un rubro que pidió una empresa (EN-011; la pantalla para pedirlo es HU-013). Tabla "rubros_solicitados".

    La ingesta descarga los rubros piloto más los solicitados activos. No guarda costos ni perfiles (RN-001).
    """
    code: str                                   # familia UNSPSC de 4 dígitos, por ejemplo "3011"
    company: str = ""                           # empresa que lo pidió
    name: Optional[str] = None                  # nombre para mostrar en el filtro de convocatorias
    requested_at: Optional[datetime] = None
    active: bool = True                         # False: ya no se descarga (sus convocatorias pasan a no vigentes)


@dataclass
class RubroCatalog:
    """Un rubro del catálogo (HU-013): familia UNSPSC con convocatorias de bienes en el último mes.
    Tabla "catalogo_rubros"; la ingesta la rehace en cada ejecución.
    """
    code: str                                   # familia UNSPSC de 4 dígitos
    name: Optional[str] = None                  # "Ej.: Cemento, hormigón premezclado" (la API no trae el nombre oficial)
    tenders_month: int = 0                      # convocatorias de bienes publicadas en los últimos 30 días
    updated_at: Optional[datetime] = None


def field_table(cls):
    """[(campo, tipo, puede_faltar)] de una entidad, para documentar el contrato."""
    rows = []
    for f in fields(cls):
        type_name = str(f.type).replace("typing.", "").replace("<class '", "").replace("'>", "")
        # Normalizar sintaxis PEP 604 en Python 3.10+ (ej. 'float | None' -> 'Optional[float]')
        if " | None" in type_name:
            type_name = f"Optional[{type_name.replace(' | None', '')}]"
        elif type_name.startswith("None | "):
            type_name = f"Optional[{type_name.replace('None | ', '')}]"
        optional = type_name.startswith("Optional") or f.default is None
        rows.append((f.name, type_name, optional))
    return rows


def example():
    """El ejemplo del contrato (una convocatoria con documentos y requisitos, y una ejecución)."""
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def tender_to_dict(tender):
    """Una convocatoria como diccionario con fechas ISO (lo que reciben las pantallas)."""
    data = asdict(tender)
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data