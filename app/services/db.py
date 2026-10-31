"""Base de datos de convocatorias (EN-007): PostgreSQL en Neon en producción, SQLite en las pruebas.

Cuatro tablas, que siguen el contrato de services/tender_contract.py:
- convocatorias: una fila por ocid; nunca se borra, solo cambia de estado.
- documentos: los documentos publicados de cada convocatoria.
- requisitos: los requisitos que extrae HU-009.
- actualizaciones: una fila por ejecución de la ingesta, con sus conteos (OE8).

No hay tabla de usuarios ni de perfiles (RN-001). La escribe la ingesta diaria de GitHub
Actions y la lee la app en Render; la cadena de conexión llega por DATABASE_URL.
"""
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
                        create_engine, event)
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
    currency: Mapped[str | None] = mapped_column(String(10))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requirements_status: Mapped[str] = mapped_column(String(30), default="pendiente")
    first_run_id: Mapped[int | None] = mapped_column(ForeignKey("actualizaciones.id"))
    last_run_id: Mapped[int | None] = mapped_column(ForeignKey("actualizaciones.id"))

    documents: Mapped[list["TenderDocument"]] = relationship(back_populates="tender", cascade="all, delete-orphan")
    requirements: Mapped[list["Requirement"]] = relationship(back_populates="tender", cascade="all, delete-orphan")


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
    """Crea las tablas que falten; no borra ni cambia las que existen."""
    Base.metadata.create_all(engine)