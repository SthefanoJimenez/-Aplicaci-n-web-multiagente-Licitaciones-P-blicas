"""Base de datos de convocatorias (EN-007): PostgreSQL en Neon en producción, SQLite en las pruebas.

Siete tablas, que siguen el contrato de services/tender_contract.py:
- convocatorias: una fila por ocid; nunca se borra, solo cambia de estado.
- documentos: los documentos publicados de cada convocatoria.
- requisitos: los requisitos que extrae HU-009.
- fragmentos: la sección de requisitos de las bases partida en fragmentos con su vector (RAG, EN-012).
  En PostgreSQL el vector usa la extensión pgvector; en SQLite (pruebas) se guarda como JSON.
- actualizaciones: una fila por ejecución de la ingesta, con sus conteos (OE8) y, desde EN-011, los
  rubros que descargó y cuántas vigentes quedaron guardadas en cada uno.
- rubros_solicitados: los rubros que piden las empresas (EN-011; la pantalla para pedirlos es HU-013).
  La ingesta descarga los rubros piloto más los solicitados activos.
- catalogo_rubros: las familias UNSPSC con convocatorias de bienes en el último mes (HU-013). La
  ingesta lo rehace en cada ejecución; es la lista de rubros del filtro y de la pantalla de rubros.

La ingesta escribe estas tablas y la app las lee; la cadena de conexión llega por DATABASE_URL.
Las cuentas y los perfiles privados de HU-008 se definen por separado en services/accounts.py.
"""
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
                        create_engine, event, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

import config


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IngestionRun(Base):
    __tablename__ = "actualizaciones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), default="en_curso")
    trigger: Mapped[str | None] = mapped_column(String(30))
    months: Mapped[str | None] = mapped_column(String(60))
    source_open_goods: Mapped[int | None] = mapped_column(Integer)
    source_open_pilot: Mapped[int | None] = mapped_column(Integer)
    saved_open: Mapped[int | None] = mapped_column(Integer)
    new_count: Mapped[int | None] = mapped_column(Integer)
    updated_count: Mapped[int | None] = mapped_column(Integer)
    closed_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    rubros: Mapped[str | None] = mapped_column(String(200))     # rubros descargados, "1510,2510,3011" (EN-011)
    per_rubro: Mapped[dict | None] = mapped_column(JSON)         # {"1510": 92, ...}: vigentes guardadas por rubro


class RubroRequest(Base):
    """Un rubro (familia UNSPSC de 4 dígitos) que pidió una empresa. Tabla "rubros_solicitados" (EN-011)."""
    __tablename__ = "rubros_solicitados"
    __table_args__ = (UniqueConstraint("code", "company", name="uq_rubro_empresa"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(4), index=True)
    name: Mapped[str | None] = mapped_column(String(200))       # nombre para mostrar en el filtro
    company: Mapped[str] = mapped_column(String(200), default="")   # quién lo pidió (HU-013 la ligará al login)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RubroCatalog(Base):
    """Un rubro del catálogo: familia UNSPSC con convocatorias de bienes en el último mes. Tabla "catalogo_rubros".

    name sale de las descripciones UNSPSC que publica la API para los ítems de esa familia (la API no
    trae el nombre de la familia); los rubros piloto usan su nombre fijo.
    """
    __tablename__ = "catalogo_rubros"

    code: Mapped[str] = mapped_column(String(4), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(300))
    tenders_month: Mapped[int] = mapped_column(Integer, default=0)    # convocatorias de bienes en 30 días
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tender(Base):
    __tablename__ = "convocatorias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ocid: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    code: Mapped[str | None] = mapped_column(String(80))
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    entity: Mapped[str | None] = mapped_column(String(300))
    department: Mapped[str | None] = mapped_column(String(80))
    method: Mapped[str | None] = mapped_column(String(120))
    rubros: Mapped[str] = mapped_column(String(80), default="")  
    state: Mapped[str] = mapped_column(String(12), default="vigente", index=True)
    item_status: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_value: Mapped[float | None] = mapped_column(Float)
    # HU-010: cantidades y clasificación de los ítems; diferida para leer bases anteriores a la migración.
    items: Mapped[list | None] = mapped_column(JSON, deferred=True)
    currency: Mapped[str | None] = mapped_column(String(10))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requirements_status: Mapped[str] = mapped_column(String(30), default="pendiente")
    first_run_id: Mapped[int | None] = mapped_column(ForeignKey("actualizaciones.id"))
    last_run_id: Mapped[int | None] = mapped_column(ForeignKey("actualizaciones.id"))

    documents: Mapped[list["TenderDocument"]] = relationship(back_populates="tender", cascade="all, delete-orphan")
    requirements: Mapped[list["Requirement"]] = relationship(back_populates="tender", cascade="all, delete-orphan")
    chunks: Mapped[list["RequirementChunk"]] = relationship(back_populates="tender", cascade="all, delete-orphan")


class TenderDocument(Base):
    __tablename__ = "documentos"
    __table_args__ = (UniqueConstraint("tender_id", "url", name="uq_documento_por_convocatoria"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("convocatorias.id"), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    doc_type: Mapped[str | None] = mapped_column(String(80))
    doc_format: Mapped[str | None] = mapped_column(String(40))
    is_bases: Mapped[bool] = mapped_column(Boolean, default=False)

    tender: Mapped[Tender] = relationship(back_populates="documents")


class Requirement(Base):
    __tablename__ = "requisitos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("convocatorias.id"), index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documentos.id"))
    requirement_type: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(10), default="texto")
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    model: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)

    tender: Mapped[Tender] = relationship(back_populates="requirements")


class RequirementChunk(Base):
    """Un fragmento de la sección de requisitos de las bases, con su vector (RAG, EN-012)."""
    __tablename__ = "fragmentos"
    __table_args__ = (UniqueConstraint("tender_id", "chunk_index", name="uq_fragmento_por_convocatoria"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("convocatorias.id"), index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documentos.id"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(10), default="texto")       # "texto" u "ocr"
    file_name: Mapped[str | None] = mapped_column(String(300))            # archivo dentro de lo descargado
    embedding = mapped_column(Vector(config.EMBEDDING_DIM).with_variant(JSON(), "sqlite"))
    model: Mapped[str | None] = mapped_column(String(80))
    tokens_est: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="chunks")


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------
def engine_url(url):
    """Neon entrega "postgresql://…" (o "postgres://…"); SQLAlchemy usa el controlador psycopg 3."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def make_engine(url=None):
    """Motor de base de datos. Sin dirección, None: la app sigue funcionando sin convocatorias."""
    url = url or config.DATABASE_URL
    if not url:
        return None
    url = engine_url(url)
    options = {"pool_pre_ping": True}   # Neon apaga el cómputo a los 5 minutos sin uso
    if url.startswith("postgresql"):
        options |= {"pool_recycle": 300, "connect_args": {"connect_timeout": 15}}
    engine = create_engine(url, **options)
    if url.startswith("sqlite"):
        # SQLite no exige las claves foráneas si no se le pide
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_tables(engine):
    """Crea las tablas que falten; no borra ni cambia las que existen.

    En PostgreSQL activa antes la extensión pgvector (Neon la ofrece en todos sus planes).
    """
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


# Columnas agregadas a tablas que ya existían en Neon: create_all no las agrega por su cuenta.
ADDED_COLUMNS = {"actualizaciones": ("rubros", "per_rubro"), "convocatorias": ("items",)}


def _add_missing_columns(engine):
    """ALTER TABLE … ADD COLUMN para las columnas nuevas (todas admiten vacío). No toca los datos."""
    from sqlalchemy import inspect
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table_name, names in ADDED_COLUMNS.items():
            existing = {c["name"] for c in inspector.get_columns(table_name)}
            table = Base.metadata.tables[table_name]
            for name in names:
                if name not in existing:
                    kind = table.c[name].type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {name} {kind}'))
