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
        stage=row.stage, stage_changed_at=as_utc(row.stage_changed_at), schedule_status=row.schedule_status,
        schedule=list(row.schedule or []), schedule_page=row.schedule_page, schedule_file=row.schedule_file,
        schedule_read_at=as_utc(row.schedule_read_at), schedule_stage=row.schedule_stage,
        schedule_due=row.schedule_due, enquiry_start=as_utc(row.enquiry_start), enquiry_end=as_utc(row.enquiry_end),
        documents=[tender_contract.TenderDocument(title=d.title, url=d.url, doc_type=d.doc_type,
                                                  doc_format=d.doc_format, is_bases=d.is_bases,
                                                  published_at=as_utc(d.published_at))
                   for d in row.documents],
        requirements=[tender_contract.Requirement(requirement_type=r.requirement_type, text=r.text, page=r.page,
                                                  source=r.source)
                      for r in row.requirements],
    )


def open_tenders(rubro=None, method=None, department=None):
    """Convocatorias vigentes, de la más reciente a la más antigua; con filtros opcionales."""
    try:
        with _session_factory()() as session:
            query = (select(db.Tender).where(db.Tender.state == "vigente")
                     .options(selectinload(db.Tender.documents), selectinload(db.Tender.requirements))
                     .order_by(db.Tender.published_at.desc()))
            if method:
                query = query.where(db.Tender.method == method)
            if department:
                query = query.where(db.Tender.department == department)
            rows = session.scalars(query).all()
            if rubro == "sin":          # bienes sin código de rubro en la API (RUBROS_INGESTA=todos)
                rows = [r for r in rows if not (r.rubros or "").split()]
            elif rubro:
                rows = [r for r in rows if rubro in (r.rubros or "").split()]
            return [_to_contract(r) for r in rows]
    except SQLAlchemyError as e:
        raise TendersUnavailable("sin_conexion") from e


def rubro_options(fallback):
    """[(código, etiqueta)] del filtro de rubros: los piloto y los solicitados activos (EN-011).

    Si la base no responde, el filtro muestra solo los piloto (fallback).
    """
    from services import rubros
    try:
        with _session_factory()() as session:
            return rubros.options(session)
    except (TendersUnavailable, SQLAlchemyError):
        return list(fallback)


def rubro_status(code):
    """Estado de un rubro para la pantalla (HU-013); None si el código no es un rubro."""
    from services import rubros
    try:
        with _session_factory()() as session:
            return rubros.status(session, code)
    except rubros.InvalidRubro:
        return None
    except SQLAlchemyError as e:
        raise TendersUnavailable("sin_conexion") from e


def request_rubro(code, company=""):
    """Guarda la solicitud de un rubro desde la pantalla (HU-013). Lanza rubros.InvalidRubro si no es válido."""
    from services import rubros
    try:
        with _session_factory().begin() as session:
            return rubros.request_rubro(session, code, company=company).code
    except SQLAlchemyError as e:
        raise TendersUnavailable("sin_conexion") from e


def rubro_catalog():
    """Filas de la pantalla de rubros: el catálogo con vigentes guardadas y estado de cada rubro."""
    from services import rubros
    try:
        with _session_factory()() as session:
            counts = rubros.open_counts(session)
            first = set(rubros.ingestion_rubros(session))
            requested = set(rubros.requested_codes(session))
            all_names = rubros.names(session)
            rows = [(r.code, r.tenders_month) for r in rubros.catalog(session)]
            rows += [(code, 0) for code in rubros.ingestion_rubros(session) if code not in {c for c, _ in rows}]
    except SQLAlchemyError as e:
        raise TendersUnavailable("sin_conexion") from e
    import config
    result = []
    for code, month in rows:
        open_count = counts.get(code, 0)
        if open_count:
            state = "con_datos"
        elif code in requested:
            state = "solicitado"
        elif code in config.PILOT_RUBROS:
            state = "sin_vigentes"          # se descarga siempre: no hace falta pedirlo
        else:
            state = "sin_datos"
        result.append({"code": code, "name": all_names.get(code), "tenders_month": month,
                       "open_count": open_count, "state": state, "priority": code in first})
    return result


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


def tender_sheet(code):
    """(convocatoria, requisitos) para la ficha (HU-010); (None, None) si el código no existe.

    Los requisitos salen de los fragmentos guardados en la base vectorial (services/requirements.py).
    """
    from services import requirements
    try:
        with _session_factory()() as session:
            row = session.scalars(select(db.Tender).where(db.Tender.code == str(code))
                                  .options(selectinload(db.Tender.documents),
                                           selectinload(db.Tender.requirements))).first()
            if row is None:
                return None, None
            return _to_contract(row), requirements.for_tender(session, row.id, row.requirements_status)
    except SQLAlchemyError as e:
        raise TendersUnavailable("sin_conexion") from e


def rubro_names():
    """{rubro: "1510 · Combustibles y lubricantes"} con el nombre del catálogo, sin conteos."""
    from services import rubros
    try:
        with _session_factory()() as session:
            names = rubros.names(session)
    except SQLAlchemyError:
        return {}
    return {code: f"{code} · {name}" for code, name in names.items()}


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


def days_since(published_at, now=None):
    """Días transcurridos desde la fecha de publicación (entero >= 0)."""
    if published_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    delta = as_utc(now) - as_utc(published_at)
    return max(0, delta.days)


def seace_url(tender):
    """Enlace público del SEACE si aplica: URL de las bases o documento de la convocatoria."""
    if not tender or not tender.documents:
        return None
    # Preferir documento de bases o primer enlace con URL válida
    bases = next((d.url for d in tender.documents if d.is_bases and d.url and d.url.startswith("http")), None)
    if bases:
        return bases
    return next((d.url for d in tender.documents if d.url and d.url.startswith("http")), None)


def bases_format(tender):
    """Formato declarado del documento que enlaza seace_url (pdf, zip, rar…), o None."""
    url = seace_url(tender)
    return next((d.doc_format for d in tender.documents if d.url == url), None) if url else None


_NORM_CATALOG = None


def match_catalog_item(title_or_desc):
    """Si el bien de la convocatoria está en el catálogo histórico, devuelve el código CUBSO; si no, None."""
    global _NORM_CATALOG
    if not title_or_desc:
        return None
    if _NORM_CATALOG is None:
        from services import data, ocds_source
        _NORM_CATALOG = [(ocds_source.normalize(row.itemcubso), int(row.codigoitem))
                         for row in data.CATALOG.itertuples()]
        _NORM_CATALOG.sort(key=lambda x: -len(x[0]))

    from services import ocds_source
    norm = ocds_source.normalize(title_or_desc)
    for c_norm, code in _NORM_CATALOG:
        if len(c_norm) >= 5 and c_norm in norm:
            return code
    return None