"""Lectura de las convocatorias desde la app (EN-007; la usan HU-007 y HU-009).

La app solo lee la base: la escribe la ingesta diaria de GitHub Actions. Las funciones
devuelven objetos del contrato (services/tender_contract.py), no filas de la base, para que
las pantallas no dependan de SQLAlchemy.

Si DATABASE_URL no está configurada o la base no responde, availability() lo dice y las
funciones de lectura lanzan TendersUnavailable: la pantalla muestra el aviso de RN-002
(templates/_tenders_notice.html) y el resto de la app, que usa los CSV históricos, sigue igual.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from services import db, tender_contract
from services.ingest import as_utc

LIMA = timezone(timedelta(hours=-5))
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "set", "oct", "nov", "dic"]

# Si la última ejecución correcta tiene más de esto, el aviso dice que los datos pueden estar atrasados
STALE_AFTER = timedelta(hours=30)

_factory = None


class TendersUnavailable(Exception):
    """No se pudo leer la base de convocatorias (sin configurar o sin conexión)."""


def _session_factory():
    global _factory
    if _factory is None:
        engine = db.make_engine()
        if engine is None:
            raise TendersUnavailable("sin_configurar")
        _factory = db.make_session_factory(engine)
    return _factory


def use_session_factory(factory):
    """Para las pruebas: usar otra base (por ejemplo, SQLite en memoria)."""
    global _factory
    _factory = factory


def _to_contract(row):
    return tender_contract.Tender(
        ocid=row.ocid, code=row.code, title=row.title, entity=row.entity, method=row.method,
        rubros=row.rubros.split() if row.rubros else [], state=row.state, description=row.description,
        department=row.department, published_at=as_utc(row.published_at), deadline_at=as_utc(row.deadline_at),
        estimated_value=row.estimated_value, currency=row.currency, item_status=row.item_status,
        first_seen_at=as_utc(row.first_seen_at), last_seen_at=as_utc(row.last_seen_at),
        closed_at=as_utc(row.closed_at), requirements_status=row.requirements_status,
        documents=[tender_contract.TenderDocument(title=d.title, url=d.url, doc_type=d.doc_type,
                                                  doc_format=d.doc_format, is_bases=d.is_bases)
                   for d in row.documents],
        requirements=[tender_contract.Requirement(requirement_type=r.requirement_type, text=r.text, page=r.page,
                                                  source=r.source)
                      for r in row.requirements],
    )


def open_tenders(rubro=None, method=None):
    """Convocatorias vigentes, de la más reciente a la más antigua; con filtros opcionales."""
    try:
        with _session_factory()() as session:
            query = (select(db.Tender).where(db.Tender.state == "vigente")
                     .options(selectinload(db.Tender.documents), selectinload(db.Tender.requirements))
                     .order_by(db.Tender.published_at.desc()))
            if method:
                query = query.where(db.Tender.method == method)
            rows = session.scalars(query).all()
            if rubro:
                rows = [r for r in rows if rubro in (r.rubros or "").split()]
            return [_to_contract(r) for r in rows]
    except SQLAlchemyError as e:
        raise TendersUnavailable("sin_conexion") from e


def tender_by_code(code):
    """Una convocatoria por su código (tender.id), vigente o no; None si no existe."""
    try:
        with _session_factory()() as session:
            row = session.scalars(select(db.Tender).where(db.Tender.code == str(code))
                                  .options(selectinload(db.Tender.documents),
                                           selectinload(db.Tender.requirements))).first()
            return _to_contract(row) if row else None
    except SQLAlchemyError as e:
        raise TendersUnavailable("sin_conexion") from e


def lima_text(moment):
    """28 oct 2026, 08:46 (hora de Lima)."""
    if moment is None:
        return None
    local = as_utc(moment).astimezone(LIMA)
    return f"{local.day} {MONTHS[local.month - 1]} {local.year}, {local:%H:%M}"


def availability(now=None):
    """Estado para el aviso de RN-002.

    {"ok": bool, "reason": None | "sin_configurar" | "sin_conexion",
     "last_update": datetime | None (última ejecución correcta), "last_update_text": str | None,
     "stale": bool, "last_failed": bool}
    """
    now = now or datetime.now(timezone.utc)
    state = {"ok": False, "reason": None, "last_update": None, "last_update_text": None,
             "stale": False, "last_failed": False}
    try:
        with _session_factory()() as session:
            last_ok = session.scalars(select(db.IngestionRun).where(db.IngestionRun.status == "ok")
                                      .order_by(db.IngestionRun.started_at.desc())).first()
            last_any = session.scalars(select(db.IngestionRun)
                                       .order_by(db.IngestionRun.started_at.desc())).first()
    except TendersUnavailable as e:
        state["reason"] = str(e)
        return state
    except SQLAlchemyError:
        state["reason"] = "sin_conexion"
        return state
    state["ok"] = True
    if last_ok:
        state["last_update"] = as_utc(last_ok.finished_at or last_ok.started_at)
        state["last_update_text"] = lima_text(state["last_update"])
        state["stale"] = now - state["last_update"] > STALE_AFTER
    state["last_failed"] = bool(last_any and last_any.status == "fallida")
    return state