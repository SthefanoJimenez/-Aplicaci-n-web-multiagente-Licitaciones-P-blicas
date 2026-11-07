
import re
import time

from sqlalchemy import case, or_, select
from sqlalchemy.orm import selectinload

import config
from services import bases_download, bases_reader, db, llm

CHUNK_CHARS = 1200        # unos 300 tokens por fragmento
CHUNK_OVERLAP = 200       # para no cortar un requisito entre dos fragmentos
MIN_CHUNK_CHARS = 80
RETRY_STATUSES = ("pendiente", "error")


class IndexingStopped(Exception):
    """Gemini dejó de responder (cuota o caída): se detiene la indexación y se reintenta mañana."""


# ---------------------------------------------------------------------------
# Fragmentos
# ---------------------------------------------------------------------------
def _cut_point(text, start, end):
    """Corta al final de una línea o de una oración, si hay una en el último tercio del fragmento."""
    if end >= len(text):
        return len(text)
    window_start = start + (end - start) * 2 // 3
    for pattern in ("\n", ". ", "; "):
        cut = text.rfind(pattern, window_start, end)
        if cut != -1:
            return cut + len(pattern)
    return end


def chunk_section(section, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """[(texto, página)] de la sección: fragmentos de ~size caracteres que se solapan en overlap.

    Ningún fragmento cruza un cambio de página, y cada uno lleva la página de su texto nuevo (lo que no
    repite del anterior). Así la ficha de la convocatoria muestra cada parte en su página exacta.
    Antes (hasta el 07/11/2026) la página era la del primer carácter y un fragmento podía seguir en la
    página siguiente.
    """
    text = section.text
    breaks = sorted(position for position, _page in section.page_marks if 0 < position < len(text))
    pieces, start, fresh = [], 0, 0          # fresh: donde empieza el texto que este fragmento agrega
    while fresh < len(text):
        limit = start + size
        page_break = next((b for b in breaks if fresh < b < limit), None)
        end = page_break if page_break is not None else _cut_point(text, start, limit)
        piece = re.sub(r"[ \t]+", " ", text[start:end]).strip()
        if text[fresh:end].strip():          # nunca se descarta texto nuevo, aunque sea poco
            pieces.append((piece, section.page_at(fresh)))
        if end >= len(text):
            break
        start, fresh = max(end - overlap, start + 1), end
    return pieces


# ---------------------------------------------------------------------------
# Indexación
# ---------------------------------------------------------------------------
def _bases_of(tender):
    return next((d for d in tender.documents if d.is_bases and d.url), None)


def _download(url, fetch):
    file = fetch(url)
    return b"".join(file.chunks)


def index_tender(session, tender, fetch=None, embed=None, use_ocr=None):
    """Lee las bases de una convocatoria y guarda sus fragmentos. Devuelve un resumen (dict).

    Lanza IndexingStopped si Gemini no responde, para no gastar el resto de la ejecución.
    """
    fetch = fetch or (lambda url: bases_download.open_bases(url, max_bytes=config.RAG_MAX_BASES_MB * 1024 * 1024))
    embed = embed or llm.embed_texts
    document = _bases_of(tender)
    if document is None:
        tender.requirements_status = "no_legible"
        return {"status": "no_legible", "chunks": 0, "tokens": 0, "detail": "la convocatoria no publica bases"}
    try:
        content = _download(document.url, fetch)
    except bases_download.BasesUnavailable as e:
        tender.requirements_status = "error"
        return {"status": "error", "chunks": 0, "tokens": 0, "detail": e.reason}
    result = bases_reader.read_bases(content, use_ocr=use_ocr)
    if result.status != "extraido":
        if tender.requirements_status == "extraido" and tender.chunks:
            # Una lectura peor no borra una buena: pasó el 07/11/2026 al releer en una PC sin OCR unas bases
            # escaneadas que la iMac sí había leído. (Si llegan otras bases, la ingesta deja la convocatoria
            # en "pendiente" y entonces sí se reemplaza todo.)
            return {"status": "conservado", "chunks": 0, "tokens": 0,
                    "detail": f"se conserva la lectura anterior; esta vez: {result.status} ({result.detail})"}
        tender.chunks.clear()
        if result.status == "no_legible" and "no hay OCR" in result.detail:
            # Falta OCR en esta computadora, no es que las bases no se puedan leer: queda para reintentar
            tender.requirements_status = "error"
            return {"status": "error", "chunks": 0, "tokens": 0,
                    "detail": result.detail + "; se reintentará en una computadora con OCR"}
        tender.requirements_status = result.status
        return {"status": result.status, "chunks": 0, "tokens": 0, "detail": result.detail}
    tender.chunks.clear()                                   # si las bases cambiaron, se reemplaza todo
    session.flush()                                         # borra los anteriores antes de insertar (clave única)
    pieces = chunk_section(result.section)
    try:
        vectors = embed([p for p, _ in pieces], "RETRIEVAL_DOCUMENT")
    except llm.ModelError as e:
        raise IndexingStopped(str(e)) from e
    tokens = 0
    for i, ((piece, page), vector) in enumerate(zip(pieces, vectors)):
        estimate = round(len(piece) / bases_reader.CHARS_PER_TOKEN)
        tokens += estimate
        tender.chunks.append(db.RequirementChunk(
            document_id=document.id, chunk_index=i, text=piece, page=page, source=result.section.source,
            file_name=result.section.document[:300], embedding=vector, model=config.EMBEDDING_MODEL,
            tokens_est=estimate))
    tender.requirements_status = "extraido"
    return {"status": "extraido", "chunks": len(pieces), "tokens": tokens, "detail": result.section.source}


def reindex_codes(session_factory, codes, fetch=None, embed=None, use_ocr=None, progress=None):
    """Vuelve a leer las bases de las convocatorias indicadas (por ejemplo, tras mejorar el lector)."""
    summary = {"processed": 0, "extraido": 0, "seccion_no_encontrada": 0, "no_legible": 0, "error": 0, "conservado": 0,
               "chunks": 0, "tokens": 0, "stopped": None, "details": []}
    with session_factory() as session:
        ids = list(session.scalars(select(db.Tender.id).where(db.Tender.code.in_([str(c) for c in codes]))))
    for number, tender_id in enumerate(ids, 1):
        started = time.perf_counter()
        try:
            with session_factory.begin() as session:
                tender = session.get(db.Tender, tender_id, options=[selectinload(db.Tender.documents),
                                                                    selectinload(db.Tender.chunks)])
                result = index_tender(session, tender, fetch=fetch, embed=embed, use_ocr=use_ocr)
                code = tender.code
        except IndexingStopped as e:
            summary["stopped"] = str(e)
            break
        summary["processed"] += 1
        summary[result["status"]] += 1
        summary["chunks"] += result["chunks"]
        summary["tokens"] += result["tokens"]
        summary["details"].append((code, result["status"], result["detail"]))
        if progress:
            progress(number, len(ids), code, result["status"], time.perf_counter() - started)
    summary["remaining"] = 0
    return summary


def pending_ids(session, limit):
    """Convocatorias vigentes por indexar. Orden: primero las de los rubros piloto y solicitados (EN-011:
    con RUBROS_INGESTA=todos hay muchas más bases que tiempo para leerlas), luego las nuevas ("pendiente")
    antes que las que fallaron, y de la más reciente a la más antigua."""
    from services import rubros
    # Los rubros se guardan como "1510 3011": todos de 4 dígitos, así que '%1510%' solo calza con 1510
    priority = [db.Tender.rubros.like(f"%{code}%") for code in rubros.ingestion_rubros(session)]
    first = case((or_(*priority), 0), else_=1) if priority else None
    order = ([first] if first is not None else []) + [
        case((db.Tender.requirements_status == "pendiente", 0), else_=1), db.Tender.published_at.desc()]
    query = (select(db.Tender.id).where(db.Tender.state == "vigente")
             .where(db.Tender.requirements_status.in_(RETRY_STATUSES)).order_by(*order))
    if limit:
        query = query.limit(limit)
    return list(session.scalars(query))


def index_pending(session_factory, limit=None, fetch=None, embed=None, use_ocr=None, progress=None):
    """Indexa hasta `limit` convocatorias, cada una en su propia transacción. Devuelve el resumen.

    progress(número, total, código, estado, segundos): se llama al terminar cada base, para mostrar el avance.
    """
    limit = config.RAG_MAX_BASES_PER_RUN if limit is None else limit
    summary = {"processed": 0, "extraido": 0, "seccion_no_encontrada": 0, "no_legible": 0, "error": 0, "conservado": 0,
               "chunks": 0, "tokens": 0, "stopped": None, "details": []}
    with session_factory() as session:
        ids = pending_ids(session, limit)
    for number, tender_id in enumerate(ids, 1):
        started = time.perf_counter()
        try:
            with session_factory.begin() as session:
                tender = session.get(db.Tender, tender_id, options=[selectinload(db.Tender.documents),
                                                                    selectinload(db.Tender.chunks)])
                result = index_tender(session, tender, fetch=fetch, embed=embed, use_ocr=use_ocr)
                code = tender.code
        except IndexingStopped as e:
            summary["stopped"] = str(e)
            break
        except Exception as e:      # un archivo raro no debe detener las demás: queda en "error"
            with session_factory.begin() as session:
                tender = session.get(db.Tender, tender_id)
                tender.requirements_status = "error"
                code = tender.code
            result = {"status": "error", "chunks": 0, "tokens": 0, "detail": f"{type(e).__name__}: {e}"[:200]}
        summary["processed"] += 1
        summary[result["status"]] += 1
        summary["chunks"] += result["chunks"]
        summary["tokens"] += result["tokens"]
        summary["details"].append((code, result["status"], result["detail"]))
        if progress:
            progress(number, len(ids), code, result["status"], time.perf_counter() - started)
    with session_factory() as session:
        summary["remaining"] = len(pending_ids(session, None))
    return summary


# ---------------------------------------------------------------------------
# Búsqueda
# ---------------------------------------------------------------------------
def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def search(session, query, k=5, tender_ids=None, codes=None, embed=None):
    """Los k fragmentos más parecidos a la pregunta, de convocatorias vigentes.

    tender_ids o codes limitan la búsqueda a ciertas convocatorias. Cada resultado trae el código,
    el título, la entidad, la página de las bases, el texto y la similitud (de 0 a 1).
    """
    embed = embed or llm.embed_texts
    vector = embed([query], "RETRIEVAL_QUERY")[0]
    chunk, tender = db.RequirementChunk, db.Tender
    filters = [tender.state == "vigente"]
    if tender_ids:
        filters.append(tender.id.in_(list(tender_ids)))
    if codes:
        filters.append(tender.code.in_([str(c) for c in codes]))
    if session.get_bind().dialect.name == "postgresql":
        distance = chunk.embedding.cosine_distance(vector).label("distance")
        rows = session.execute(select(chunk, tender, distance).join(tender, chunk.tender_id == tender.id)
                               .where(*filters).order_by(distance).limit(k)).all()
        scored = [(c, t, 1 - d) for c, t, d in rows]
    else:
        rows = session.execute(select(chunk, tender).join(tender, chunk.tender_id == tender.id).where(*filters)).all()
        scored = sorted(((c, t, _cosine(vector, c.embedding)) for c, t in rows), key=lambda r: -r[2])[:k]
    bases = {}
    for c, t, _ in scored:
        if c.document_id and c.document_id not in bases:
            doc = session.get(db.TenderDocument, c.document_id)
            bases[c.document_id] = doc.url if doc else None
    return [{"code": t.code, "title": t.title, "entity": t.entity, "page": c.page, "text": c.text,
             "source": c.source, "file_name": c.file_name, "bases_url": bases.get(c.document_id),
             "similarity": round(float(score), 4)} for c, t, score in scored]