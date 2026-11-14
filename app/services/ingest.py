"""Ingesta diaria de convocatorias vigentes (EN-007).

Una ejecución:
1. registra el inicio en "actualizaciones";
2. descarga los archivos mensuales de la API OCDS (mes actual y anterior, configurable);
3. se queda con los bienes vigentes (algún ítem CONVOCADO) de los rubros de la ingesta: los piloto
   más los que solicitaron las empresas (EN-011, services/rubros.py), o de todos los rubros si
   RUBROS_INGESTA=todos;
4. en UNA transacción: crea o actualiza cada convocatoria por ocid, agrega sus documentos y
   pasa a "no_vigente" las que la fuente ya no trae vigentes (no se borran);
5. registra el resultado, los conteos de OE8 y cuántas vigentes quedaron guardadas por rubro, y rehace
   el catálogo de rubros con las convocatorias de bienes del último mes (HU-013).

Si la fuente falla o algo sale mal a mitad, la transacción se deshace: quedan los datos de la
ejecución anterior y la ejecución figura como "fallida" con su error.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

import config
from services import db, ocds_source, schedule
from services import rubros as rubros_service


def as_utc(value):
    """SQLite devuelve las fechas sin zona; en la base se guardan siempre en UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def pilot_open_rows(rows, now, rubros=None):
    """(bienes vigentes de todo el país, vigentes de los rubros pedidos) según las reglas del SP-005.

    rubros: los de la ingesta; si no se indican, los piloto.
    """
    rubros = set(rubros or config.PILOT_RUBROS)
    open_goods = [r for r in rows if ocds_source.is_goods(r) and ocds_source.is_open(r, now)]
    pilot = [r for r in open_goods if set(ocds_source.rubros_of(r)) & rubros and r.get("ocid")]
    return open_goods, pilot


def rubros_by_volume(open_goods, first=()):
    """Todos los rubros de los bienes vigentes: primero `first` (piloto y solicitados), luego de mayor a menor."""
    from collections import Counter
    counts = Counter(f for r in open_goods if r.get("ocid") for f in set(ocds_source.rubros_of(r)))
    first = list(dict.fromkeys(first))
    return first + [c for c, _ in counts.most_common() if c not in first]


def count_by_rubro(rubros_text, codes, with_no_rubro=False):
    """{rubro: cuántas filas lo incluyen}. rubros_text: los campo "rubros" ("1510 2510"). Una convocatoria
    con ítems de dos rubros cuenta en los dos. with_no_rubro: agrega "sin" con las que no tienen rubro."""
    counts = {code: 0 for code in codes}
    if with_no_rubro:
        counts[rubros_service.NO_RUBRO] = 0
    for text in rubros_text:
        found = set((text or "").split())
        for code in found & set(codes):
            counts[code] += 1
        if with_no_rubro and not found:
            counts[rubros_service.NO_RUBRO] += 1
    return counts


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
    enquiry_start, enquiry_end = row.get("enquiry") or (None, None)      # HU-018
    tender.enquiry_start = as_utc(ocds_source.parse_date(enquiry_start))
    tender.enquiry_end = as_utc(ocds_source.parse_date(enquiry_end))
    tender.estimated_value = row.get("value")
    tender.items = row.get("items") or []
    tender.currency = row.get("currency")
    tender.last_seen_at = now
    tender.last_run_id = run_id


def _sync_documents(tender, row):
    """Agrega los documentos nuevos y marca cuál son las bases. Los que la fuente ya no lista se conservan."""
    bases = ocds_source.bases_document(row)
    previous = next((d.url for d in tender.documents if d.is_bases), None)
    if bases and previous and bases.get("url") != previous:
        tender.requirements_status = "pendiente"      # llegaron otras bases (p. ej. las integradas): el RAG las vuelve a leer
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
        current.published_at = as_utc(ocds_source.parse_date(doc.get("published")))
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


def run_ingestion(session_factory, now=None, fetch=None, months=None, rubros=None, trigger=None, all_rubros=None):
    """Ejecuta la ingesta y devuelve un resumen (dict). No lanza excepciones por fallas de la fuente.

    rubros: lista fija (pruebas); si no se indica, la ingesta usa los piloto y los solicitados activos.
    all_rubros: True guarda todos los rubros de bienes de la fuente (por defecto, config.ALL_RUBROS).
    """
    all_rubros = config.ALL_RUBROS if all_rubros is None else all_rubros
    now_lima = now or datetime.now(ocds_source.LIMA)
    now_utc = now_lima.astimezone(timezone.utc)
    fetch = fetch or ocds_source.fetch_rows
    months = months or config.OCDS_MONTHS

    with session_factory.begin() as session:
        codes = [rubros_service.normalize_code(c) for c in rubros] if rubros else rubros_service.ingestion_rubros(session)
        run = db.IngestionRun(started_at=now_utc, status="en_curso", trigger=trigger,
                              rubros="todos" if all_rubros else ",".join(codes),
                              months=",".join(f"{y}-{m:02d}" for y, m in ocds_source.months_to_fetch(now_lima, months)))
        session.add(run)
        session.flush()
        run_id = run.id

    if not codes and not all_rubros:
        # Sin esta guarda, una lista vacía dejaría todas las convocatorias como "no_vigente"
        error = ("no hay rubros para descargar: PILOT_RUBROS está vacío y no hay rubros solicitados activos. "
                 "No se cambió ninguna convocatoria.")
        _finish_failed(session_factory, run_id, error)
        return {"run_id": run_id, "status": "fallida", "error": error, "rubros": codes}

    try:
        fetched = fetch(now_lima, months)
        rows = fetched[0]
        # Meses que el portal aún no publica (por ejemplo, el día 1 del mes): se informan, no fallan
        skipped = list(fetched[2]) if len(fetched) > 2 else []
    except ocds_source.SourceError as error:
        _finish_failed(session_factory, run_id, error)
        return {"run_id": run_id, "status": "fallida", "error": str(error), "http_status": error.status,
                "rubros": codes}

    if all_rubros:
        codes = rubros_by_volume(pilot_open_rows(rows, now_lima, codes or ("0000",))[0], first=codes)
    open_goods, pilot = pilot_open_rows(rows, now_lima, codes)
    if all_rubros:
        # Todos: también los bienes cuyos ítems no traen código de clasificación (no tienen rubro)
        pilot = [r for r in open_goods if r.get("ocid")]
    source_by_rubro = count_by_rubro([" ".join(ocds_source.rubros_of(r)) for r in pilot], codes, all_rubros)
    try:
        with session_factory.begin() as session:
            open_ocids = {r["ocid"] for r in pilot}
            # Los documentos se traen junto con las convocatorias: antes se pedían de a una (una consulta por
            # convocatoria), y con todos los rubros eran ~1 700 idas y vueltas a Neon (ejecución 15: 481 s)
            existing = {t.ocid: t for t in session.query(db.Tender).options(selectinload(db.Tender.documents))
                        .filter((db.Tender.ocid.in_(open_ocids)) | (db.Tender.state == "vigente")).all()}
            new_count = updated_count = closed_count = documents_added = stage_changes = 0
            new_rubros = []
            for row in pilot:
                tender = existing.get(row["ocid"])
                if tender is None:
                    tender = db.Tender(ocid=row["ocid"], first_seen_at=now_utc, first_run_id=run_id)
                    session.add(tender)
                    existing[row["ocid"]] = tender
                    new_count += 1
                    new_rubros.append(" ".join(ocds_source.rubros_of(row)))
                else:
                    updated_count += 1
                tender.state = "vigente"
                tender.closed_at = None
                _apply_row(tender, row, now_utc, run_id)
                documents_added += _sync_documents(tender, row)
                # HU-018: etapa según los documentos; si llegó el pliego o las bases integradas, el
                # cronograma queda para leerse otra vez (services/schedule.py)
                previous_stage = tender.stage
                if schedule.mark_stage(tender, tender.documents, now_utc) and previous_stage is not None:
                    stage_changes += 1
            for ocid, tender in existing.items():
                if ocid not in open_ocids and tender.state == "vigente":
                    tender.state = "no_vigente"
                    tender.closed_at = now_utc
                    closed_count += 1
            session.flush()
            saved_open = session.query(db.Tender).filter(db.Tender.state == "vigente").count()
            saved_by_rubro = count_by_rubro(
                session.scalars(select(db.Tender.rubros).where(db.Tender.state == "vigente")), codes, all_rubros)
            run = session.get(db.IngestionRun, run_id)
            run.status = "ok"
            run.finished_at = now_utc if now else db.utcnow()
            run.source_open_goods = len(open_goods)
            run.source_open_pilot = len(pilot)
            run.saved_open = saved_open
            run.new_count, run.updated_count, run.closed_count = new_count, updated_count, closed_count
            run.per_rubro = saved_by_rubro
            # Catálogo de rubros (HU-013): familias con convocatorias de bienes en el último mes
            catalog_size = rubros_service.save_catalog(
                session, rubros_service.catalog_from_rows(rows, now_lima), now_utc)
            if all_rubros:
                run.rubros = f"todos ({len(codes)})"
    except Exception as error:   # la transacción ya se deshizo: quedan los datos anteriores
        _finish_failed(session_factory, run_id, f"{type(error).__name__}: {error}")
        return {"run_id": run_id, "status": "fallida", "error": f"{type(error).__name__}: {error}", "rubros": codes}

    new_by_rubro = count_by_rubro(new_rubros, codes, all_rubros)
    return {"run_id": run_id, "status": "ok", "source_open_goods": len(open_goods), "source_open_pilot": len(pilot),
            "saved_open": saved_open, "new": new_count, "updated": updated_count, "closed": closed_count,
            "documents_added": documents_added, "stage_changes": stage_changes, "skipped_months": skipped, "rubros": codes, "all_rubros": all_rubros,
            "catalog": catalog_size,
            # por rubro: (vigentes en la fuente, vigentes guardadas tras la ejecución, nuevas en esta ejecución)
            "by_rubro": {c: (source_by_rubro[c], saved_by_rubro[c], new_by_rubro[c]) for c in saved_by_rubro}}
