"""Ingesta diaria de convocatorias vigentes (EN-007).

Una ejecución:
1. registra el inicio en "actualizaciones";
2. descarga los archivos mensuales de la API OCDS (mes actual y anterior, configurable);
3. se queda con los bienes vigentes (algún ítem CONVOCADO) de los rubros piloto;
4. en UNA transacción: crea o actualiza cada convocatoria por ocid, agrega sus documentos y
   pasa a "no_vigente" las que la fuente ya no trae vigentes (no se borran);
5. registra el resultado y los conteos de OE8.

Si la fuente falla o algo sale mal a mitad, la transacción se deshace: quedan los datos de la
ejecución anterior y la ejecución figura como "fallida" con su error.
"""
from datetime import datetime, timezone

import config
from services import db, ocds_source


def as_utc(value):
    """SQLite devuelve las fechas sin zona; en la base se guardan siempre en UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def pilot_open_rows(rows, now, rubros=None):
    """(bienes vigentes de todo el país, vigentes de los rubros piloto) según las reglas del SP-005."""
    rubros = set(rubros or config.PILOT_RUBROS)
    open_goods = [r for r in rows if ocds_source.is_goods(r) and ocds_source.is_open(r, now)]
    pilot = [r for r in open_goods if set(ocds_source.rubros_of(r)) & rubros and r.get("ocid")]
    return open_goods, pilot


def _apply_row(tender, row, now, run_id):
    """Copia a la fila de la base los campos del contrato que trae la fuente."""
    tender.code = str(row["code"]) if row.get("code") is not None else None
    tender.title = row.get("title")
    tender.description = row.get("description")
    tender.entity = row.get("entity")
    tender.department = row.get("department")
    tender.method = row.get("method")
    tender.rubros = " ".join(ocds_source.rubros_of(row))
    tender.item_status = (row.get("item_status") or None)
    tender.published_at = as_utc(ocds_source.parse_date(row.get("published")))
    tender.deadline_at = as_utc(ocds_source.parse_date(row.get("deadline")))
    tender.estimated_value = row.get("value")
    tender.currency = row.get("currency")
    tender.last_seen_at = now
    tender.last_run_id = run_id


def _sync_documents(tender, row):
    """Agrega los documentos nuevos y marca cuál son las bases. Los que la fuente ya no lista se conservan."""
    bases = ocds_source.bases_document(row)
    by_url = {d.url: d for d in tender.documents}
    added = 0
    for doc in row.get("documents") or []:
        url = doc.get("url")
        if not url:
            continue
        current = by_url.get(url)
        if current is None:
            current = db.TenderDocument(url=url)
            tender.documents.append(current)
            by_url[url] = current
            added += 1
        current.title = doc.get("title")
        current.doc_type = doc.get("type")
        current.doc_format = doc.get("format")
    for url, current in by_url.items():
        current.is_bases = bool(bases and url == bases.get("url"))
    return added


def _scrub(text):
    """El error se guarda en la base: nunca con la cadena de conexión ni su contraseña."""
    from urllib.parse import urlsplit
    if config.DATABASE_URL:
        text = text.replace(config.DATABASE_URL, "[DATABASE_URL]")
        password = urlsplit(config.DATABASE_URL).password
        if password:
            text = text.replace(password, "[contraseña]")
    return text


def _finish_failed(session_factory, run_id, error):
    with session_factory.begin() as session:
        run = session.get(db.IngestionRun, run_id)
        run.status = "fallida"
        run.finished_at = db.utcnow()
        run.error = _scrub(str(error))[:2000]


def run_ingestion(session_factory, now=None, fetch=None, months=None, rubros=None, trigger=None):
    """Ejecuta la ingesta y devuelve un resumen (dict). No lanza excepciones por fallas de la fuente."""
    now_lima = now or datetime.now(ocds_source.LIMA)
    now_utc = now_lima.astimezone(timezone.utc)
    fetch = fetch or ocds_source.fetch_rows
    months = months or config.OCDS_MONTHS

    with session_factory.begin() as session:
        run = db.IngestionRun(started_at=now_utc, status="en_curso", trigger=trigger,
                              months=",".join(f"{y}-{m:02d}" for y, m in ocds_source.months_to_fetch(now_lima, months)))
        session.add(run)
        session.flush()
        run_id = run.id

    try:
        rows, _files = fetch(now_lima, months)
    except ocds_source.SourceError as error:
        _finish_failed(session_factory, run_id, error)
        return {"run_id": run_id, "status": "fallida", "error": str(error), "http_status": error.status}

    open_goods, pilot = pilot_open_rows(rows, now_lima, rubros)
    try:
        with session_factory.begin() as session:
            open_ocids = {r["ocid"] for r in pilot}
            existing = {t.ocid: t for t in session.query(db.Tender).filter(
                (db.Tender.ocid.in_(open_ocids)) | (db.Tender.state == "vigente")).all()}
            new_count = updated_count = closed_count = documents_added = 0
            for row in pilot:
                tender = existing.get(row["ocid"])
                if tender is None:
                    tender = db.Tender(ocid=row["ocid"], first_seen_at=now_utc, first_run_id=run_id)
                    session.add(tender)
                    existing[row["ocid"]] = tender
                    new_count += 1
                else:
                    updated_count += 1
                tender.state = "vigente"
                tender.closed_at = None
                _apply_row(tender, row, now_utc, run_id)
                documents_added += _sync_documents(tender, row)
            for ocid, tender in existing.items():
                if ocid not in open_ocids and tender.state == "vigente":
                    tender.state = "no_vigente"
                    tender.closed_at = now_utc
                    closed_count += 1
            session.flush()
            saved_open = session.query(db.Tender).filter(db.Tender.state == "vigente").count()
            run = session.get(db.IngestionRun, run_id)
            run.status = "ok"
            run.finished_at = db.utcnow()
            run.source_open_goods = len(open_goods)
            run.source_open_pilot = len(pilot)
            run.saved_open = saved_open
            run.new_count, run.updated_count, run.closed_count = new_count, updated_count, closed_count
    except Exception as error:   # la transacción ya se deshizo: quedan los datos anteriores
        _finish_failed(session_factory, run_id, f"{type(error).__name__}: {error}")
        return {"run_id": run_id, "status": "fallida", "error": f"{type(error).__name__}: {error}"}

    return {"run_id": run_id, "status": "ok", "source_open_goods": len(open_goods), "source_open_pilot": len(pilot),
            "saved_open": saved_open, "new": new_count, "updated": updated_count, "closed": closed_count,
            "documents_added": documents_added}