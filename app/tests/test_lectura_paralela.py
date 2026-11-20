"""Lectura de bases y cronogramas en paralelo, en una sola computadora (20/11/2026).

Con la ingesta de todos los rubros hay unas 4,500 bases vigentes; leídas una por una tardaban cerca de
dos días. RAG_HILOS (o --hilos) lee varias a la vez. No usan internet ni Gemini.
"""
import threading
import time

import pytest
from sqlalchemy import create_engine, event

import config
import ingest_job
from services import db, llm, rag
from test_en012 import fake_embed, fake_fetch, seed, statuses


@pytest.fixture
def factory(tmp_path):
    """Base en archivo: SQLite en memoria no se comparte entre hilos."""
    engine = create_engine(f"sqlite:///{tmp_path / 'bases.db'}", connect_args={"timeout": 30})
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    db.create_tables(engine)
    yield db.make_session_factory(engine)
    engine.dispose()


class Concurrency:
    """fetch simulado que tarda un poco y anota cuántas descargas hubo a la vez."""
    def __init__(self, delay=0.15):
        self.delay, self.now, self.peak, self.lock = delay, 0, 0, threading.Lock()

    def __call__(self, url):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        try:
            time.sleep(self.delay)
            return fake_fetch(url)
        finally:
            with self.lock:
                self.now -= 1


def test_varias_a_la_vez_dan_el_mismo_resultado_que_una_por_una(factory):
    seed(factory)
    fetch = Concurrency()
    summary = rag.index_pending(factory, limit=0, fetch=fetch, embed=fake_embed, use_ocr=False, workers=4)
    assert fetch.peak > 1                                        # sí hubo descargas simultáneas
    assert statuses(factory) == {"1001": "extraido", "1002": "extraido", "1003": "seccion_no_encontrada",
                                 "1004": "error", "1005": "error", "1006": "no_legible"}
    assert summary["processed"] == 6 and summary["extraido"] == 2 and summary["remaining"] == 2
    assert sorted(code for code, _s, _d in summary["details"]) == ["1001", "1002", "1003", "1004", "1005", "1006"]
    with factory() as session:
        assert summary["tokens"] == sum(c.tokens_est for c in session.query(db.RequirementChunk))


def test_el_avance_se_informa_una_vez_por_base(factory):
    seed(factory)
    seen = []
    rag.index_pending(factory, limit=0, fetch=Concurrency(0.05), embed=fake_embed, use_ocr=False, workers=3,
                      progress=lambda number, total, code, status, seconds: seen.append((number, total, code)))
    assert [n for n, _t, _c in seen] == [1, 2, 3, 4, 5, 6] and {t for _n, t, _c in seen} == {6}
    assert sorted(c for _n, _t, c in seen) == ["1001", "1002", "1003", "1004", "1005", "1006"]


def test_sin_cuota_de_gemini_se_detiene_sin_perder_lo_pendiente(factory):
    seed(factory, codes=("1001", "1002", "1003"))

    def broken(texts, task_type):
        raise llm.ModelError("El modelo respondió con el error 429", code=429)

    summary = rag.index_pending(factory, limit=0, fetch=Concurrency(0.05), embed=broken, use_ocr=False, workers=4)
    assert summary["stopped"]
    states = statuses(factory)
    assert states["1001"] == states["1002"] == "pendiente"      # quedan para la próxima ejecución
    with factory() as session:
        assert session.query(db.RequirementChunk).count() == 0


def test_cronogramas_en_paralelo(factory):
    seed(factory)
    with factory.begin() as session:
        for tender in session.query(db.Tender):
            tender.schedule_due = True
    fetch = Concurrency(0.05)
    summary = rag.read_due_schedules(factory, limit=0, fetch=fetch, workers=4)
    assert fetch.peak > 1 and summary["processed"] == 6
    assert summary["sin_bases"] == 1 and summary["error"] == 1   # 1006 sin bases; 1005 no se pudo descargar
    assert summary["remaining"] == 1                             # 1005 se reintenta


def test_releer_codigos_en_paralelo_no_se_detiene_por_un_archivo_raro(factory, monkeypatch):
    seed(factory, codes=("1001", "1002"))
    original = rag.index_tender

    def sometimes_broken(session, tender, **kwargs):
        if tender.code == "1002":
            raise RuntimeError("archivo raro")
        return original(session, tender, **kwargs)

    monkeypatch.setattr(rag, "index_tender", sometimes_broken)
    summary = rag.reindex_codes(factory, ["1001", "1002"], fetch=fake_fetch, embed=fake_embed, use_ocr=False, workers=2)
    assert statuses(factory) == {"1001": "extraido", "1002": "error"} and summary["processed"] == 2


def test_opciones_de_la_consola(monkeypatch, capsys):
    for name in ("RAG_WORKERS", "RAG_MAX_BASES_PER_RUN", "SCHEDULE_MAX_PER_RUN"):
        monkeypatch.setattr(config, name, getattr(config, name))      # se restauran al terminar
    assert ingest_job.main_with(["--hilos", "9", "--solo-rag"]) == 2
    assert "entre 1 y 8" in capsys.readouterr().out
    with pytest.raises(SystemExit):                                   # la opción de dos computadoras ya no existe
        ingest_job.parse(["--parte", "1/2"])
    capsys.readouterr()
    options = ingest_job.parse(["--hilos", "6", "--sin-limite"])
    assert ingest_job.apply_reading_options(options) is None
    assert (config.RAG_WORKERS, config.RAG_MAX_BASES_PER_RUN, config.SCHEDULE_MAX_PER_RUN) == (6, 0, 0)
    assert ingest_job.reading_mode() == " (6 a la vez)"
    config.RAG_WORKERS = 1
    assert ingest_job.reading_mode() == ""


def test_un_limite_por_minuto_de_gemini_se_espera_y_no_detiene_la_lectura(factory, monkeypatch, capsys):
    """20/11/2026: con 6 a la vez la lectura se detuvo en la base 34 por un 429 pasajero de Gemini."""
    seed(factory, codes=("1001", "1002"))
    monkeypatch.setattr(rag, "EMBED_WAITS", (0, 0))
    calls = []

    def busy_once(texts, task_type):
        calls.append(1)
        if len(calls) == 1:
            raise llm.ModelError("El modelo respondió con el error 429", code=429)
        return fake_embed(texts, task_type)

    summary = rag.index_pending(factory, limit=0, fetch=fake_fetch, embed=busy_once, use_ocr=False, workers=2)
    assert not summary["stopped"] and summary["extraido"] == 2
    assert "fragmentos por minuto" in capsys.readouterr().out


def test_si_gemini_sigue_sin_responder_se_detiene_tras_las_esperas(factory, monkeypatch):
    seed(factory, codes=("1001",))
    monkeypatch.setattr(rag, "EMBED_WAITS", (0, 0))
    calls = []

    def quota(texts, task_type):
        calls.append(1)
        raise llm.ModelError("El modelo respondió con el error 429", code=429)

    summary = rag.index_pending(factory, limit=0, fetch=fake_fetch, embed=quota, use_ocr=False)
    assert summary["stopped"] and len(calls) == 3 and statuses(factory)["1001"] == "pendiente"


def test_una_clave_invalida_se_detiene_sin_esperar(factory, monkeypatch):
    seed(factory, codes=("1001",))
    monkeypatch.setattr(rag, "EMBED_WAITS", (60, 60))         # si esperara, la prueba tardaría 2 minutos
    calls = []

    def invalid(texts, task_type):
        calls.append(1)
        raise llm.ModelError("El modelo gemini-embedding-001 respondió con el error 403", code=403)

    summary = rag.index_pending(factory, limit=0, fetch=fake_fetch, embed=invalid, use_ocr=False)
    assert summary["stopped"] and len(calls) == 1


def test_si_la_lectura_se_detuvo_no_se_leen_todos_los_cronogramas_aparte(monkeypatch):
    monkeypatch.setattr(ingest_job, "RAG_STOPPED", True)
    monkeypatch.setattr(config, "SCHEDULE_MAX_PER_RUN", 0)
    lines = ingest_job.run_schedules(factory=None)
    assert "se leerán junto con las bases" in lines[0]


def test_con_un_429_el_tope_baja_a_lo_que_gemini_acepto_y_luego_sube():
    pace = rag._GeminiPace(per_minute=0)
    for _ in range(10):
        pace.wait_turn(5)                                 # 50 fragmentos en el último minuto, sin tope
    assert pace.slow_down(30) == (True, 40)              # el tope pasa a ~80 % de lo enviado
    assert pace.slow_down(30) == (False, 40)             # otro hilo con el mismo aviso: no se cuenta dos veces
    pace.went_well()
    assert pace.per_minute == 41


def test_el_tope_por_minuto_se_respeta_entre_todos_los_hilos():
    pace = rag._GeminiPace(per_minute=6, window=0.3)
    moments = []

    def ask():
        pace.wait_turn(3)
        moments.append(time.monotonic())

    threads = [threading.Thread(target=ask) for _ in range(4)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    moments.sort()
    assert moments[1] - started < 0.2                     # los 2 primeros (6 fragmentos) pasan juntos
    assert moments[2] - started >= 0.25                   # los otros esperan a que se libere la ventana


def test_el_tope_inicial_se_puede_fijar_en_el_env(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_VECTORS_PER_MINUTE", 90)
    assert rag._GeminiPace().per_minute == 90


def test_mientras_se_esperan_los_vectores_no_queda_una_conexion_abierta(tmp_path):
    """20/11/2026: con las esperas por el límite de Gemini, la conexión a Neon abierta durante minutos se
    cortaba y la base quedaba en «error». Ahora la descarga, el OCR y los vectores van sin conexión."""
    engine = create_engine(f"sqlite:///{tmp_path / 'bases.db'}", connect_args={"timeout": 30})
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    seed(factory, codes=("1001", "1002"))
    open_while_embedding = []

    def watching(texts, task_type):
        open_while_embedding.append(engine.pool.checkedout())
        return fake_embed(texts, task_type)

    summary = rag.index_pending(factory, limit=0, fetch=fake_fetch, embed=watching, use_ocr=False)
    assert summary["extraido"] == 2 and open_while_embedding == [0, 0]
    engine.dispose()


def test_la_lectura_reparte_las_bases_entre_bienes_servicios_y_obras(factory):
    """20/11/2026: con todas las categorías, si la lectura se corta las tres deben quedar parejas.
    Siempre sigue la categoría con menos bases leídas; dentro de ella, la más reciente."""
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = [("b1", "bienes", "extraido", 0), ("b2", "bienes", "extraido", 0),      # ya leídas: bienes va adelantado
            ("b3", "bienes", "pendiente", 9), ("b4", "bienes", "pendiente", 8),
            ("s1", "servicios", "pendiente", 5), ("s2", "servicios", "pendiente", 7), ("s3", "servicios", "pendiente", 1),
            ("o1", "obras", "pendiente", 3), ("o2", "obras", "pendiente", 2),
            ("x1", None, "pendiente", 4)]                                         # sin categoría: cuenta como bienes
    with factory.begin() as session:
        for code, category, status, day in rows:
            session.add(db.Tender(ocid=f"ocds-x-{code}", code=code, title=code, entity="E", state="vigente",
                                  category=category, requirements_status=status,
                                  published_at=base + timedelta(days=day)))
    with factory() as session:
        order = [session.get(db.Tender, i).code for i in rag.pending_ids(session, None)]
        assert order == ["s2", "o1", "s1", "o2", "b3", "s3", "b4", "x1"]
        assert [session.get(db.Tender, i).code for i in rag.pending_ids(session, 3)] == ["s2", "o1", "s1"]
    line = ingest_job.category_line(factory)
    assert line == "Vigentes con requisitos leídos, por categoría: bienes 2 de 5 · servicios 0 de 3 · obras 0 de 2"