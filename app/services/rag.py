import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import selectinload

import config
from services import bases_download, bases_facts, bases_reader, db, llm, schedule

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


# Ritmo de Gemini para los vectores (20/11/2026). La primera ejecución con 6 bases a la vez se detuvo en la
# base 34 por un 429 (límite por minuto: un vector de prueba funcionaba al mismo tiempo, y el límite es por
# proyecto, no por clave). Ahora el ritmo se aprende solo, sin tocar RAG_HILOS:
# - todos los hilos piden vectores por un mismo turno (_PACE), que cuenta los fragmentos enviados en el último
#   minuto;
# - con un 429, todos esperan (EMBED_WAITS) y el tope pasa a ~80 % de lo que se envió en el último minuto;
#   con cada respuesta correcta el tope sube de a 1;
# - GEMINI_VECTORES_POR_MINUTO fija un tope desde el inicio (por ejemplo, el que muestra AI Studio);
# - si una base sigue sin vectores tras todas las esperas (unos 5,5 min), la lectura se detiene como antes
#   y lo pendiente queda para la próxima ejecución; con una clave inválida (400/401/403), de inmediato.
EMBED_WAITS = (30, 60, 60, 60, 60, 60)
MIN_PER_MINUTE = 10
STOP_NOW_CODES = (400, 401, 403)


class _GeminiPace:
    """Turno compartido por los hilos para pedir vectores a Gemini, con un tope de fragmentos por minuto."""

    def __init__(self, per_minute=None, window=60.0):
        self.lock = threading.Lock()
        self.window = window
        self.sent = deque()          # (momento, fragmentos) enviados dentro de la ventana
        self.per_minute = config.GEMINI_VECTORS_PER_MINUTE if per_minute is None else per_minute   # 0 = sin tope
        self.paused_until = 0.0

    def _used(self, now):
        while self.sent and now - self.sent[0][0] >= self.window:
            self.sent.popleft()
        return sum(n for _, n in self.sent)

    def wait_turn(self, n=1):
        while True:
            with self.lock:
                now = time.monotonic()
                used = self._used(now)
                wait = self.paused_until - now
                if wait <= 0:
                    if not self.per_minute or not self.sent or used + n <= self.per_minute:
                        self.sent.append((now, n))
                        return
                    wait = self.sent[0][0] + self.window - now
            time.sleep(max(wait, 0.05))

    def slow_down(self, pause):
        """Gemini respondió 429: todos esperan `pause` s y el tope baja. Devuelve (es un aviso nuevo, tope)."""
        with self.lock:
            now = time.monotonic()
            new = self.paused_until <= now + 1          # no había una pausa en curso (varios hilos avisan a la vez)
            self.paused_until = max(self.paused_until, now + pause)
            if new:
                guess = max(MIN_PER_MINUTE, int(self._used(now) * 0.8))
                self.per_minute = min(self.per_minute, guess) if self.per_minute else guess
            return new, self.per_minute

    def went_well(self):
        with self.lock:
            if self.per_minute:
                self.per_minute += 1


_PACE = _GeminiPace()


def _no_vectors(error):
    """Se acabó la cuota de vectores: las bases se siguen leyendo y se guardan sin vectores (20/11/2026)."""
    if llm.mark_exhausted("vectores"):
        print(f"  Gemini se quedó sin cuota para los vectores ({error}): las bases se siguen leyendo y se guardan "
              "sin vectores; se completan con --completar-vectores cuando haya cuota.", flush=True)
    return None


def _embed_patiently(embed, texts):
    """Vectores de los textos, o None si Gemini se quedó sin cuota (la lectura sigue sin vectores).

    Lanza IndexingStopped solo si la clave no sirve (400, 401, 403): ahí no tiene sentido seguir.
    """
    if llm.exhausted("vectores"):
        return None
    for wait in EMBED_WAITS + (None,):
        _PACE.wait_turn(len(texts))
        try:
            vectors = embed(texts, "RETRIEVAL_DOCUMENT")
        except llm.ModelError as e:
            if e.code in STOP_NOW_CODES and "embeddings" not in str(e):
                raise IndexingStopped(str(e)) from e
            if wait is None or llm.out_of_quota(e):
                return _no_vectors(e)
            if llm.exhausted("vectores"):          # otro hilo ya lo supo
                return None
            new, limit = _PACE.slow_down(wait)
            if new:
                print(f"  Gemini pidió bajar el ritmo ({e}): las lecturas esperan {wait} s y luego envían como "
                      f"máximo {limit} fragmentos por minuto (sube solo si Gemini responde bien)…", flush=True)
            continue
        _PACE.went_well()
        return vectors


def prepare_bases(url, fetch=None, embed=None, use_ocr=None, analyze=None, category=None):
    """Lo lento de leer unas bases, sin tocar la base de datos: descarga, lectura (con OCR) y vectores.

    Devuelve (resultado de la lectura, fragmentos, vectores, requisitos analizados o None). Lanza
    BasesUnavailable si no se pudo descargar e IndexingStopped si Gemini no da los vectores. Separado el
    20/11/2026: con las esperas por el límite de Gemini, una conexión a Neon abierta durante minutos se
    cortaba y la base quedaba en «error».

    analyze: función (sección, contexto, categoría, páginas) → dict; por defecto bases_facts.analyze si está
    activo. Si falla, la lectura se guarda igual y el análisis queda para --analizar-requisitos.
    """
    fetch = fetch or (lambda u: bases_download.open_bases(u, max_bytes=config.RAG_MAX_BASES_MB * 1024 * 1024))
    embed = embed or llm.embed_texts
    result = bases_reader.read_bases(_download(url, fetch), use_ocr=use_ocr)
    if result.status != "extraido":
        return result, [], [], None
    pieces = chunk_section(result.section)
    vectors = _embed_patiently(embed, [p for p, _ in pieces])
    return result, pieces, vectors, _analyze_section(result, analyze, category)


def _analyze_section(result, analyze=None, category=None):
    """Requisitos analizados (bases_facts) de una lectura, o None si no se analiza o Gemini falla."""
    if analyze is None and (not bases_facts.enabled() or llm.exhausted("texto")):
        return None
    analyze = analyze or bases_facts.analyze
    pages = sorted({page for _pos, page in result.section.page_marks}) or None
    try:
        return analyze(bases_facts.marked_section(result.section), result.context, category, pages)
    except llm.ModelError as e:
        if llm.out_of_quota(e) and llm.mark_exhausted("texto"):
            print(f"  Gemini se quedó sin cuota ({e}): el análisis de requisitos queda para "
                  "--analizar-requisitos.", flush=True)
        return None


def index_tender(session, tender, fetch=None, embed=None, use_ocr=None, prepared=None):
    """Lee las bases de una convocatoria y guarda sus fragmentos. Devuelve un resumen (dict).

    prepared: lo que devolvió prepare_bases, si ya se leyó fuera de la transacción.
    Lanza IndexingStopped si Gemini no responde, para no gastar el resto de la ejecución.
    """
    document = _bases_of(tender)
    if document is None:
        tender.requirements_status = "no_legible"
        return {"status": "no_legible", "chunks": 0, "tokens": 0, "detail": "la convocatoria no publica bases"}
    if prepared is None:
        try:
            prepared = prepare_bases(document.url, fetch=fetch, embed=embed, use_ocr=use_ocr)
        except bases_download.BasesUnavailable as e:
            tender.requirements_status = "error"
            return {"status": "error", "chunks": 0, "tokens": 0, "detail": e.reason}
    result, pieces, vectors, facts = prepared if len(prepared) == 4 else (*prepared, None)
    if bases_reader.gemini_ocr_enabled() and not getattr(result, "gemini_failed", False):
        # Leída con Gemini disponible (o sin páginas escaneadas): no hace falta volver a releerla con Gemini
        tender.gemini_read_at = db.utcnow()
    if result.schedule is not None:          # HU-018: el cronograma sale de la misma lectura
        schedule.save(tender, result.schedule, document, db.utcnow())
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
    tokens = 0
    vectors = vectors if vectors is not None else [None] * len(pieces)     # sin cuota: se completan después
    for i, ((piece, page), vector) in enumerate(zip(pieces, vectors)):
        estimate = round(len(piece) / bases_reader.CHARS_PER_TOKEN)
        tokens += estimate
        tender.chunks.append(db.RequirementChunk(
            document_id=document.id, chunk_index=i, text=piece, page=page, source=result.section.source,
            ocr_engine=getattr(result.section, "ocr_engine", None),
            file_name=result.section.document[:300], embedding=vector,
            model=config.EMBEDDING_MODEL if vector is not None else None,
            tokens_est=estimate))
    tender.requirements_status = "extraido"
    # Los requisitos analizados son de estas bases: si no se pudieron analizar, se borra el análisis anterior
    bases_facts.stamp(tender, facts)
    return {"status": "extraido", "chunks": len(pieces), "tokens": tokens,
            "detail": result.section.source + ("" if any(v is not None for v in vectors) or not pieces
                                               else "; sin vectores"),
            "analyzed": facts is not None}


def _each(ids, one, workers):
    """Genera (resultado, segundos) de one(id) para cada id, en orden de término.

    workers > 1: varias a la vez (20/11/2026). Casi todo el tiempo de una base es la descarga desde el SEACE,
    así que leer varias a la vez acorta la ejecución. Cada una usa su propia sesión y su propia transacción.
    Si one lanza IndexingStopped (Gemini sin cuota), no empieza ninguna más; las que estaban en curso
    terminan y se guardan, y después sale la excepción. Con Ctrl+C pasa lo mismo.
    """
    if workers <= 1:
        for item in ids:
            started = time.perf_counter()
            yield one(item), time.perf_counter() - started
        return
    stop = threading.Event()

    def guarded(item):
        if stop.is_set():
            return None
        started = time.perf_counter()
        try:
            return one(item), time.perf_counter() - started
        except IndexingStopped:
            stop.set()
            raise

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bases")
    stopped = None
    try:
        for future in as_completed([pool.submit(guarded, item) for item in ids]):
            try:
                outcome = future.result()
            except IndexingStopped as e:
                stopped = stopped or e
                continue
            if outcome is not None:
                yield outcome
    except BaseException:
        stop.set()                      # Ctrl+C: no empieza ninguna más
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    if stopped is not None:
        raise stopped


def _workers(workers):
    return max(1, int(config.RAG_WORKERS if workers is None else workers))


def _new_summary():
    return {"processed": 0, "extraido": 0, "seccion_no_encontrada": 0, "no_legible": 0, "error": 0, "conservado": 0,
            "chunks": 0, "tokens": 0, "stopped": None, "details": []}


def _index_ids(session_factory, ids, fetch, embed, use_ocr, progress, workers):
    """Lee las bases de las convocatorias `ids` (cada una en su propia transacción) y devuelve el resumen."""
    summary = _new_summary()

    def one(tender_id):
        try:
            # 1. Dirección de las bases (consulta corta). 2. Descarga, OCR y vectores sin conexión abierta
            # a la base: pueden tardar minutos. 3. Se guarda todo en una transacción corta.
            with session_factory() as session:
                tender = session.get(db.Tender, tender_id, options=[selectinload(db.Tender.documents)])
                document = _bases_of(tender)
                url = document.url if document else None
                category = tender.category
            prepared, unavailable = None, None
            if url:
                try:
                    prepared = prepare_bases(url, fetch=fetch, embed=embed, use_ocr=use_ocr, category=category)
                except bases_download.BasesUnavailable as e:
                    unavailable = e.reason
            with session_factory.begin() as session:
                tender = session.get(db.Tender, tender_id, options=[selectinload(db.Tender.documents),
                                                                    selectinload(db.Tender.chunks)])
                if unavailable is not None:
                    tender.requirements_status = "error"
                    return tender.code, {"status": "error", "chunks": 0, "tokens": 0, "detail": unavailable}
                current = _bases_of(tender)
                if prepared is not None and (current is None or current.url != url):
                    prepared = None          # cambiaron las bases mientras se leían: se leen de nuevo aquí
                result = index_tender(session, tender, fetch=fetch, embed=embed, use_ocr=use_ocr, prepared=prepared)
                return tender.code, result
        except IndexingStopped:
            raise
        except Exception as e:      # un archivo raro no debe detener las demás: queda en "error"
            with session_factory.begin() as session:
                tender = session.get(db.Tender, tender_id)
                tender.requirements_status = "error"
                code = tender.code
            return code, {"status": "error", "chunks": 0, "tokens": 0, "detail": f"{type(e).__name__}: {e}"[:200]}

    try:
        for number, ((code, result), seconds) in enumerate(_each(ids, one, _workers(workers)), 1):
            summary["processed"] += 1
            summary[result["status"]] += 1
            summary["chunks"] += result["chunks"]
            summary["tokens"] += result["tokens"]
            summary["details"].append((code, result["status"], result["detail"]))
            if progress:
                progress(number, len(ids), code, result["status"], seconds)
    except IndexingStopped as e:
        summary["stopped"] = str(e)
    return summary


def reindex_codes(session_factory, codes, fetch=None, embed=None, use_ocr=None, progress=None, workers=None):
    """Vuelve a leer las bases de las convocatorias indicadas (por ejemplo, tras mejorar el lector)."""
    with session_factory() as session:
        ids = list(session.scalars(select(db.Tender.id).where(db.Tender.code.in_([str(c) for c in codes]))))
    summary = _index_ids(session_factory, ids, fetch, embed, use_ocr, progress, workers)
    summary["remaining"] = 0
    return summary


CATEGORY_ORDER = ("bienes", "servicios", "obras")


def _priority_rubros(session):
    """Condiciones de los rubros que se leen primero: piloto y solicitados, solo con RUBROS_INGESTA=solicitados."""
    if config.ALL_RUBROS:
        return []
    from services import rubros
    # Los rubros se guardan como "1510 3011": todos de 4 dígitos, así que '%1510%' solo calza con 1510
    return [db.Tender.rubros.like(f"%{code}%") for code in rubros.ingestion_rubros(session)]


def pending_ids(session, limit):
    """Convocatorias vigentes por indexar.

    Orden: las nuevas ("pendiente") antes que las que fallaron. Dentro de cada grupo se reparten entre
    bienes, servicios y obras (20/11/2026): siempre sigue la categoría que tiene menos bases ya leídas, de la
    más reciente a la más antigua. Así, si la lectura se corta (cuota de Gemini), las tres categorías quedan
    parejas. Con RUBROS_INGESTA=todos los rubros piloto y solicitados ya no van primero (20/11/2026); con
    RUBROS_INGESTA=solicitados sí, porque solo se guardan esos.
    """
    priority = _priority_rubros(session)
    first = case((or_(*priority), 0), else_=1) if priority else literal(1)
    retry = case((db.Tender.requirements_status == "pendiente", 0), else_=1)
    rows = session.execute(
        select(db.Tender.id, db.Tender.category, first, retry)
        .where(db.Tender.state == "vigente").where(db.Tender.requirements_status.in_(RETRY_STATUSES))
        .order_by(first, retry, db.Tender.published_at.desc(), db.Tender.id.desc())).all()
    category = func.coalesce(db.Tender.category, "bienes")       # las anteriores sin categoría eran bienes
    read = dict(session.execute(
        select(category, func.count()).where(db.Tender.state == "vigente")
        .where(db.Tender.requirements_status.not_in(RETRY_STATUSES)).group_by(category)).all())
    ordered, groups = [], {}
    for tender_id, cat, group_a, group_b in rows:
        groups.setdefault((group_a, group_b), {}).setdefault(cat or "bienes", deque()).append(tender_id)
    for key in sorted(groups):
        queues = groups[key]
        while queues:
            cat = min(queues, key=lambda c: (read.get(c, 0), CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 9))
            ordered.append(queues[cat].popleft())
            read[cat] = read.get(cat, 0) + 1
            if not queues[cat]:
                del queues[cat]
            if limit and len(ordered) >= limit:
                return ordered
    return ordered


def index_pending(session_factory, limit=None, fetch=None, embed=None, use_ocr=None, progress=None, workers=None):
    """Indexa hasta `limit` convocatorias, cada una en su propia transacción. Devuelve el resumen.

    progress(número, total, código, estado, segundos): se llama al terminar cada base, para mostrar el avance.
    workers: cuántas a la vez (por defecto config.RAG_WORKERS).
    """
    limit = config.RAG_MAX_BASES_PER_RUN if limit is None else limit
    with session_factory() as session:
        ids = pending_ids(session, limit)
    summary = _index_ids(session_factory, ids, fetch, embed, use_ocr, progress, workers)
    with session_factory() as session:
        summary["remaining"] = len(pending_ids(session, None))
    return summary


# ---------------------------------------------------------------------------
# Cronograma (HU-018)
# ---------------------------------------------------------------------------
def due_schedule_ids(session, limit=None):
    """Convocatorias vigentes cuyo cronograma hay que leer: nunca se leyó o cambió la etapa. De la más
    reciente a la más antigua (con RUBROS_INGESTA=solicitados, primero los rubros piloto y solicitados)."""
    priority = _priority_rubros(session)
    first = case((or_(*priority), 0), else_=1) if priority else None
    order = ([first] if first is not None else []) + [db.Tender.stage_changed_at.desc(), db.Tender.published_at.desc()]
    query = (select(db.Tender.id).where(db.Tender.state == "vigente")
             .where(db.Tender.schedule_due.is_(True)).order_by(*order))
    if limit:
        query = query.limit(limit)
    return list(session.scalars(query))


def read_due_schedules(session_factory, limit=None, fetch=None, progress=None, workers=None):
    """Lee el cronograma de las bases de hasta `limit` convocatorias (sin OCR ni Gemini). Devuelve el resumen.

    Si la descarga falla, la convocatoria queda para la siguiente ejecución; nunca se guarda una fecha
    que no esté escrita en las bases.
    """
    limit = config.SCHEDULE_MAX_PER_RUN if limit is None else limit
    fetch = fetch or (lambda url: bases_download.open_bases(url, max_bytes=config.RAG_MAX_BASES_MB * 1024 * 1024))
    summary = {"processed": 0, "error": 0, "sin_bases": 0, "details": []}
    summary.update({status: 0 for status in schedule.SCHEDULE_STATUSES})
    with session_factory() as session:
        ids = due_schedule_ids(session, limit)

    def one(tender_id):
        code = None
        try:
            with session_factory.begin() as session:
                tender = session.get(db.Tender, tender_id, options=[selectinload(db.Tender.documents)])
                code, document = tender.code, _bases_of(tender)
                if document is None:
                    tender.schedule_due = False
                    return code, "sin_bases", "la convocatoria no publica bases"
                try:
                    content = _download(document.url, fetch)
                except bases_download.BasesUnavailable as e:
                    return code, "error", e.reason            # queda para la siguiente ejecución
                result = bases_reader.read_schedule(content)
                schedule.save(tender, result, document, db.utcnow())
                return code, result.status, result.detail
        except Exception as e:      # un archivo raro no detiene las demás
            return code, "error", f"{type(e).__name__}: {e}"[:200]

    for number, ((code, status, detail), seconds) in enumerate(_each(ids, one, _workers(workers)), 1):
        summary["processed"] += 1
        summary[status] += 1
        summary["details"].append((code, status, detail))
        if progress:
            progress(number, len(ids), code, status, seconds)
    with session_factory() as session:
        summary["remaining"] = len(due_schedule_ids(session))
    return summary


# ---------------------------------------------------------------------------
# Búsqueda
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Análisis de los requisitos ya leídos (20/11/2026)
# ---------------------------------------------------------------------------
def training_codes(session, limit=None):
    """Convocatorias vigentes de servicios ya analizadas antes de que el análisis trajera la capacitación del
    personal clave (24/11/2026) y cuyas bases leídas la mencionan. Solo esas se vuelven a analizar: medición del
    24/11/2026, 25 de 58 bases de servicios la mencionan; en bienes y obras casi nunca."""
    from sqlalchemy import exists
    from sqlalchemy.orm import undefer
    from services import bases_facts
    mentions = exists().where(db.RequirementChunk.tender_id == db.Tender.id,
                              db.RequirementChunk.text.ilike("%capacitaci%"))
    query = (select(db.Tender).options(undefer(db.Tender.bases_facts))
             .where(db.Tender.state == "vigente", db.Tender.category == "servicios",
                    db.Tender.requirements_status == "extraido", db.Tender.bases_facts_at.is_not(None), mentions)
             .order_by(db.Tender.deadline_at.is_(None), db.Tender.deadline_at, db.Tender.id))
    codes = [t.code for t in session.scalars(query)
             if t.code and int((t.bases_facts or {}).get("version") or 1) < bases_facts.TRAINING_VERSION]
    return codes[:limit] if limit else codes


def unanalyzed_ids(session, limit=None, codes=None):
    """Convocatorias vigentes con requisitos leídos y sin analizar (o las de `codes`, aunque ya lo estén)."""
    query = select(db.Tender.id).where(db.Tender.requirements_status == "extraido")
    if codes:
        query = query.where(db.Tender.code.in_([str(c) for c in codes]))
    else:
        query = query.where(db.Tender.state == "vigente").where(db.Tender.bases_facts_at.is_(None))
    query = query.order_by(db.Tender.deadline_at.is_(None), db.Tender.deadline_at, db.Tender.id)
    if limit:
        query = query.limit(limit)
    return list(session.scalars(query))


ANALYSIS_WAITS = (30, 60, 60)
_TEXT_PACE = _GeminiPace(per_minute=0)


def quota_reason(error):
    """« (límite: GenerateRequestsPerMinute…)» con el nombre del límite que dio Gemini, si lo dice."""
    detail = getattr(error, "detail", "") or ""
    found = re.search(r"quota_?(?:id|metric)['\"]?\s*[:=]\s*['\"]?([\w./-]+)", detail, re.I) or \
        re.search(r"(?:quota exceeded for(?: metric)?|for metric)[:\s]+([\w./-]+)", detail, re.I)
    return f" (límite: {found.group(1)})" if found else ""


def analyze_saved(session_factory, limit=None, codes=None, fetch=None, analyze=None, progress=None, workers=None,
                  download=True):
    """Analiza con Gemini los requisitos de bases ya leídas (sin volver a hacer OCR ni vectores).

    La sección sale de los fragmentos guardados; las otras partes (cuantía, consorcio…) se toman de las bases
    descargadas de nuevo, sin OCR (download=False: solo la sección). Si Gemini deja de responder (límite
    diario), se detiene y las demás quedan para la próxima vez.
    """
    from services import requirements
    analyze = analyze or bases_facts.analyze
    fetch = fetch or (lambda u: bases_download.open_bases(u, max_bytes=config.RAG_MAX_BASES_MB * 1024 * 1024))
    with session_factory() as session:
        ids = unanalyzed_ids(session, limit, codes)
        remaining = len(unanalyzed_ids(session)) if not codes else 0
    summary = {"processed": 0, "analizado": 0, "error": 0, "requisitos": 0, "stopped": None, "details": [],
               "remaining": remaining}
    if llm.exhausted("texto"):
        summary["stopped"] = "Gemini ya se había quedado sin cuota en esta ejecución"
        return summary

    def one(tender_id):
        with session_factory() as session:
            tender = session.get(db.Tender, tender_id, options=[selectinload(db.Tender.documents),
                                                                selectinload(db.Tender.chunks)])
            code, category = tender.code, tender.category
            blocks = requirements.merge_chunks([(c.page, c.text) for c in sorted(tender.chunks, key=lambda c: c.chunk_index)])
            document = _bases_of(tender)
            url = document.url if document else None
        context = ""
        if download and url:
            try:
                context = bases_reader.context_of(_download(url, fetch))
            except Exception:          # sin las otras partes se analiza igual la sección
                context = ""
        pages = sorted({b.page for b in blocks if b.page}) or None
        for wait in ANALYSIS_WAITS + (None,):
            _TEXT_PACE.wait_turn(1)
            try:
                facts = analyze(bases_facts.section_with_pages(blocks), context, category, pages)
                _TEXT_PACE.went_well()
                break
            except llm.ModelError as e:
                if e.code in STOP_NOW_CODES or e.code == 402 or llm.out_of_quota(e):
                    if llm.out_of_quota(e):
                        llm.mark_exhausted("texto")
                    raise IndexingStopped(f"{e}{quota_reason(e)}") from e
                if e.code != 429 or wait is None:
                    if e.code == 429:
                        raise IndexingStopped(f"{e}{quota_reason(e)}") from e
                    return code, {"status": "error", "detail": str(e)[:200], "requisitos": 0}
                # 21/11/2026: un límite por minuto (6 a la vez) no detiene el análisis: se espera y se sigue
                new, limit = _TEXT_PACE.slow_down(wait)
                if new:
                    print(f"  Gemini pidió bajar el ritmo{quota_reason(e)}: el análisis espera {wait} s y luego "
                          f"envía como máximo {limit} bases por minuto…", flush=True)
        with session_factory.begin() as session:
            bases_facts.stamp(session.get(db.Tender, tender_id), facts)
        return code, {"status": "analizado", "detail": "", "requisitos": len(facts["requisitos"])}

    try:
        for number, ((code, result), seconds) in enumerate(_each(ids, one, _workers(workers)), 1):
            summary["processed"] += 1
            summary[result["status"]] += 1
            summary["requisitos"] += result["requisitos"]
            summary["details"].append((code, result["status"], result["detail"]))
            if progress:
                progress(number, len(ids), code, result["status"], seconds)
    except IndexingStopped as e:
        summary["stopped"] = str(e)
    summary["remaining"] = max(0, summary["remaining"] - summary["analizado"])
    return summary


# ---------------------------------------------------------------------------
# Vectores que faltan (20/11/2026): bases guardadas cuando Gemini se quedó sin cuota de vectores
# ---------------------------------------------------------------------------
def missing_vectors(session):
    return session.scalar(select(func.count()).select_from(db.RequirementChunk)
                          .where(db.RequirementChunk.embedding.is_(None))) or 0


def fill_missing_vectors(session_factory, embed=None, batch=llm.EMBED_BATCH, progress=None):
    """Pide a Gemini los vectores de los fragmentos guardados sin ellos. Se detiene si sigue sin cuota."""
    embed = embed or llm.embed_texts
    done = 0
    while True:
        with session_factory() as session:
            rows = session.execute(select(db.RequirementChunk.id, db.RequirementChunk.text)
                                   .where(db.RequirementChunk.embedding.is_(None))
                                   .order_by(db.RequirementChunk.id).limit(batch)).all()
        if not rows:
            return {"filled": done, "remaining": 0, "stopped": None}
        vectors = _embed_patiently(embed, [text for _id, text in rows])
        if vectors is None:
            with session_factory() as session:
                remaining = missing_vectors(session)
            return {"filled": done, "remaining": remaining, "stopped": "Gemini sigue sin cuota para los vectores"}
        with session_factory.begin() as session:
            for (chunk_id, _text), vector in zip(rows, vectors):
                chunk = session.get(db.RequirementChunk, chunk_id)
                chunk.embedding, chunk.model = vector, config.EMBEDDING_MODEL
        done += len(rows)
        if progress:
            progress(done)


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
    filters = [tender.state == "vigente", chunk.embedding.isnot(None)]      # sin vectores aún: no se buscan
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