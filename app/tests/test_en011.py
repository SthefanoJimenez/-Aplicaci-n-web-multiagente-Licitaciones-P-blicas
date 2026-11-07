"""EN-011: ingesta de los rubros adicionales (los que solicitan las empresas).

Criterios que se prueban:
1. La lista de rubros sale de los rubros piloto y de las solicitudes guardadas.
2. Un rubro solicitado aparece con sus convocatorias después de la siguiente ingesta.
3. El resumen de cada ejecución indica cuántas vigentes se guardaron por rubro.
4. (Medición con 5 rubros) el comando --espacio mide cada tabla en PostgreSQL; la prueba con
   PostgreSQL corre solo si RAG_TEST_POSTGRES_URL apunta a una base de prueba.

Usan la API simulada (tests/fake_ocds.py) y SQLite: no necesitan internet ni Neon.
"""
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text

import config
import fake_ocds
from app import app
from services import db, ingest, ocds_source, rubros, tenders

LIMA = ocds_source.LIMA
NOW = datetime(2026, 11, 6, 8, 30, tzinfo=LIMA)


@pytest.fixture
def factory():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    yield db.make_session_factory(engine)
    engine.dispose()


def two_rubros(n, now):
    """Una convocatoria con ítems de dos rubros (1510 y 3011)."""
    row = fake_ocds.release(n, now, family="15101505")
    row["tender"]["items"].append({"description": "CEMENTO", "statusDetails": "CONVOCADO",
                                   "classification": {"scheme": "UNSPSC", "id": "30111601"}})
    return row


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(ocds_source, "REQUEST_PAUSE_SECONDS", 0)
    with fake_ocds.FakeOCDS() as fake:
        r = fake_ocds.release
        fake.months[(2026, 11)] = [
            r(201, NOW, family="15101505"),                                  # 1510 piloto
            r(202, NOW, family="25101500"),                                  # 2510 piloto
            r(203, NOW, family="30111601"),                                  # 3011 cemento
            r(204, NOW, family="30111504"),                                  # 3011 arena
            r(205, NOW, family="41116135"),                                  # 4111 instrumentos
            r(206, NOW, family="30111601", item_status="ADJUDICADO"),        # 3011 ya no vigente
            r(207, NOW, family="30111601", category="services"),             # 3011 servicio: no entra
            two_rubros(208, NOW),                                            # 1510 y 3011
        ]
        fake.months[(2026, 10)] = [r(209, NOW, family="42295513", date=NOW - timedelta(days=20))]   # 4229
        yield fake


def run(factory, fake, now=NOW):
    session = ocds_source.session_for(fake.base + "/api/v1")
    return ingest.run_ingestion(factory, now=now, fetch=lambda n, m: ocds_source.fetch_rows(n, m, session=session),
                                months=2, trigger="prueba")


def request(factory, *codes, company="Ferretería Andina SAC", name=None):
    with factory.begin() as session:
        for code in codes:
            rubros.request_rubro(session, code, company=company, name=name)


def vigentes(factory):
    with factory() as session:
        return {t.code: t.state for t in session.query(db.Tender)}


# ---------------------------------------------------------------------------
# Criterio 1: lista de rubros = piloto + solicitudes guardadas
# ---------------------------------------------------------------------------
def test_rubro_codes_are_normalized_and_invalid_ones_rejected():
    assert rubros.normalize_code("3011") == "3011"
    assert rubros.normalize_code("30111601") == "3011"          # código de ítem completo
    assert rubros.normalize_code(" 3011-1601 ") == "3011"
    for bad in ("", "30", "abc", "0000"):
        with pytest.raises(rubros.InvalidRubro):
            rubros.normalize_code(bad)


def test_ingestion_list_is_pilots_plus_active_requests_without_repeating(factory):
    with factory() as session:
        assert rubros.ingestion_rubros(session) == list(config.PILOT_RUBROS)
    request(factory, "4111", "3011", "1510")                    # 1510 ya es piloto: no se repite
    request(factory, "3011", company="Constructora Norte EIRL")   # dos empresas piden el mismo
    with factory() as session:
        assert rubros.ingestion_rubros(session) == ["1510", "2510", "3011", "4111"]
        assert len(rubros.requests_table(session)) == 4
    with factory.begin() as session:
        assert rubros.withdraw_rubro(session, "3011", company="Ferretería Andina SAC") == 1
    with factory() as session:
        assert "3011" in rubros.ingestion_rubros(session)       # otra empresa lo sigue pidiendo
    with factory.begin() as session:
        assert rubros.withdraw_rubro(session, "3011") == 1
        assert rubros.withdraw_rubro(session, "4111") == 1
    with factory() as session:
        assert rubros.ingestion_rubros(session) == ["1510", "2510"]


def test_requesting_twice_reactivates_instead_of_duplicating(factory):
    request(factory, "3011")
    with factory.begin() as session:
        rubros.withdraw_rubro(session, "3011")
    request(factory, "3011", name="Cemento y agregados")
    with factory() as session:
        table = rubros.requests_table(session)
    assert len(table) == 1 and table[0][1] == "Cemento y agregados" and table[0][4] is True


# ---------------------------------------------------------------------------
# Criterio 2: el rubro solicitado aparece con sus convocatorias tras la siguiente ingesta
# ---------------------------------------------------------------------------
def test_requested_rubro_appears_after_the_next_ingestion(factory, source):
    first = run(factory, source)
    assert first["rubros"] == ["1510", "2510"]
    assert set(vigentes(factory)) == {"201", "202", "208"}      # 208 entra por su ítem 1510

    request(factory, "3011", "4229", name=None)
    second = run(factory, source)
    assert second["rubros"] == ["1510", "2510", "3011", "4229"]
    saved = vigentes(factory)
    assert {"203", "204", "209"} <= set(saved) and all(state == "vigente" for state in saved.values())
    assert "206" not in saved and "207" not in saved             # adjudicada y servicio: no entran
    assert "205" not in saved                                    # 4111 no se pidió
    assert second["new"] == 3


def test_requested_rubro_shows_in_the_screen_filter_and_list(factory, source):
    request(factory, "3011", name="Cemento y agregados")
    run(factory, source)
    tenders.use_session_factory(factory)
    try:
        assert ("3011", "3011 · Cemento y agregados · 3 vigentes") in tenders.rubro_options([])
        assert [t.code for t in tenders.open_tenders(rubro="3011")] == ["203", "204", "208"]
        client = app.test_client()
        page = client.get("/convocatorias?rubro=3011").get_data(as_text=True)
        assert "3011 · Cemento y agregados" in page and "ADQUISICION 203" in page
    finally:
        tenders.use_session_factory(None)


def test_withdrawn_rubro_tenders_become_not_open_but_shared_ones_stay(factory, source):
    request(factory, "3011")
    run(factory, source)
    with factory.begin() as session:
        rubros.withdraw_rubro(session, "3011")
    result = run(factory, source)
    states = vigentes(factory)
    assert states["203"] == states["204"] == "no_vigente"        # no se borran
    assert states["208"] == "vigente"                            # también es del piloto 1510
    assert result["closed"] == 2


# ---------------------------------------------------------------------------
# Criterio 3: conteo por rubro en el resumen de cada ejecución
# ---------------------------------------------------------------------------
def test_summary_counts_open_tenders_saved_per_rubro_and_stores_them(factory, source):
    request(factory, "3011", "4111", "4229")
    result = run(factory, source)
    # (vigentes en la fuente, vigentes guardadas, nuevas en esta ejecución)
    assert result["by_rubro"] == {"1510": (2, 2, 2), "2510": (1, 1, 1), "3011": (3, 3, 3),
                                  "4111": (1, 1, 1), "4229": (1, 1, 1)}
    assert result["saved_open"] == 7                             # 208 cuenta en 1510 y en 3011, pero es una
    with factory() as session:
        stored = session.get(db.IngestionRun, result["run_id"])
        assert stored.rubros == "1510,2510,3011,4111,4229"
        assert stored.per_rubro == {"1510": 2, "2510": 1, "3011": 3, "4111": 1, "4229": 1}
    again = run(factory, source)
    assert again["by_rubro"]["3011"] == (3, 3, 0)                # segunda vez: ninguna nueva


def test_failed_run_records_the_rubros_it_tried(factory, source):
    source.fail = 500
    result = run(factory, source)
    assert result["status"] == "fallida" and result["rubros"] == ["1510", "2510"]
    with factory() as session:
        assert session.get(db.IngestionRun, result["run_id"]).rubros == "1510,2510"


def test_without_any_rubro_the_run_fails_and_keeps_the_open_tenders(factory, source, monkeypatch):
    run(factory, source)
    monkeypatch.setattr(config, "PILOT_RUBROS", ())         # sin piloto y sin solicitudes
    result = run(factory, source)
    assert result["status"] == "fallida" and "no hay rubros" in result["error"]
    assert set(vigentes(factory).values()) == {"vigente"}     # no se cerró ninguna
    request(factory, "3011")                                 # solo con solicitudes también funciona
    result = run(factory, source)
    assert result["status"] == "ok" and result["rubros"] == ["3011"]
    assert vigentes(factory)["201"] == "no_vigente" and vigentes(factory)["203"] == "vigente"


def test_job_requests_rubros_and_prints_the_count_per_rubro(tmp_path, monkeypatch, capsys, source):
    import ingest_job
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'base.db'}")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "resumen.md"))
    assert ingest_job.main_with(["--solicitar-rubro", "3011", "--solicitar-rubro", "4229",
                                 "--empresa", "Ferretería Andina SAC", "--nombre", "Cemento y agregados"]) == 0
    out = capsys.readouterr().out
    assert "Rubros de la ingesta (4)" in out and "Ferretería Andina SAC" in out
    assert ingest_job.main_with(["--solicitar-rubro", "12"]) == 2      # código inválido
    real_run = ingest.run_ingestion
    session = ocds_source.session_for(source.base + "/api/v1")
    monkeypatch.setattr(ingest, "run_ingestion", lambda factory, trigger=None: real_run(
        factory, now=NOW, fetch=lambda n, m: ocds_source.fetch_rows(n, m, session=session), months=2, trigger=trigger))
    assert ingest_job.main_with(["--sin-rag"]) == 0
    out = capsys.readouterr().out
    assert "4 rubros" in out and "Vigentes guardadas por rubro" in out
    assert "- 3011 · Cemento y agregados: 3 guardadas (3 en la fuente · 3 nuevas)" in out
    assert "3011 · Cemento y agregados: 3 guardadas" in (tmp_path / "resumen.md").read_text(encoding="utf-8")
    assert ingest_job.main_with(["--quitar-rubro", "3011"]) == 0
    assert "Rubros de la ingesta (3)" in capsys.readouterr().out
    assert ingest_job.main_with(["--espacio"]) == 0
    assert "solo se mide en PostgreSQL" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# RUBROS_INGESTA=todos: todos los rubros de bienes, con prioridad para piloto y solicitados
# ---------------------------------------------------------------------------
def test_all_rubros_mode_saves_every_goods_rubro_and_lists_pilots_first(factory, source, monkeypatch):
    request(factory, "4229")
    monkeypatch.setattr(config, "ALL_RUBROS", True)
    result = run(factory, source)
    assert result["status"] == "ok" and result["all_rubros"] is True
    assert result["rubros"][:3] == ["1510", "2510", "4229"]                 # prioridad, luego por volumen
    assert result["rubros"][3] == "3011" and set(result["rubros"]) == {"1510", "2510", "4229", "3011", "4111"}
    assert set(vigentes(factory)) == {"201", "202", "203", "204", "205", "208", "209"}   # sin adjudicada ni servicio
    assert result["by_rubro"]["4111"] == (1, 1, 1)
    with factory() as session:
        stored = session.get(db.IngestionRun, result["run_id"])
        assert stored.rubros == "todos (5)" and stored.per_rubro["3011"] == 3
        labels = dict(rubros.options(session))
    assert labels["4111"] == "4111 · Rubro 4111 · 1 vigente"
    assert labels["1510"] == "1510 · Combustibles y lubricantes · 2 vigentes"


def test_all_rubros_mode_also_saves_goods_without_a_classification_code(factory, source, monkeypatch):
    unclassified = fake_ocds.release(210, NOW)
    for item in unclassified["tender"]["items"]:
        item.pop("classification", None)                 # la API no publica el código UNSPSC del ítem
    source.months[(2026, 11)].append(unclassified)
    result = run(factory, source)
    assert "210" not in vigentes(factory)                # con la lista de rubros no hay cómo ubicarlo
    monkeypatch.setattr(config, "ALL_RUBROS", True)
    result = run(factory, source)
    assert vigentes(factory)["210"] == "vigente"
    assert result["source_open_pilot"] == result["source_open_goods"] == 8      # cobertura completa
    assert result["by_rubro"]["sin"] == (1, 1, 1)
    tenders.use_session_factory(factory)
    try:
        assert ("sin", "Sin rubro (la API no publica su código) · 1 vigente") in tenders.rubro_options([])
        assert [t.code for t in tenders.open_tenders(rubro="sin")] == ["210"]
    finally:
        tenders.use_session_factory(None)


def test_all_rubros_mode_reads_bases_of_pilots_and_requests_first(factory, source, monkeypatch):
    from services import rag
    monkeypatch.setattr(config, "ALL_RUBROS", True)
    run(factory, source)
    with factory() as session:
        codes = [session.get(db.Tender, i).code for i in rag.pending_ids(session, None)]
    assert set(codes[:3]) == {"201", "202", "208"}                           # 1510 y 2510 primero
    request(factory, "4111")
    with factory() as session:
        codes = [session.get(db.Tender, i).code for i in rag.pending_ids(session, None)]
    assert set(codes[:4]) == {"201", "202", "205", "208"} and set(codes[4:]) == {"203", "204", "209"}


def test_all_rubros_summary_shows_the_largest_and_totals_the_rest(tmp_path, monkeypatch, capsys, source):
    import ingest_job
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'base.db'}")
    monkeypatch.setattr(config, "ALL_RUBROS", True)
    monkeypatch.setattr(ingest_job, "RUBRO_LINES", 3)
    real_run = ingest.run_ingestion
    session = ocds_source.session_for(source.base + "/api/v1")
    monkeypatch.setattr(ingest, "run_ingestion", lambda factory, trigger=None: real_run(
        factory, now=NOW, fetch=lambda n, m: ocds_source.fetch_rows(n, m, session=session), months=2, trigger=trigger))
    assert ingest_job.main_with(["--sin-rag"]) == 0
    out = capsys.readouterr().out
    assert "rubros: todos los de bienes (prioridad: 1510, 2510)" in out
    assert "- 3011: 3 guardadas" in out                                     # el mayor de los demás
    assert "- … y 2 rubros más con 2 vigentes guardadas" in out
    assert "Cobertura: 7 de 7 bienes vigentes de la fuente, en 5 rubros · todos cargados." in out
    assert ingest_job.main_with(["--solicitar-rubro", "4229"]) == 0
    assert "hoy ya hay 1 vigente guardada de este rubro" in capsys.readouterr().out
    assert ingest_job.main_with(["--solicitar-rubro", "9999"]) == 0
    assert "hoy no hay vigentes guardadas de este rubro" in capsys.readouterr().out


def test_rerun_does_not_query_documents_one_tender_at_a_time(factory, monkeypatch):
    """Ejecución 15 (06/11): 1 681 convocatorias actualizadas tardaron 481 s por pedir sus documentos de a una."""
    from sqlalchemy import event
    monkeypatch.setattr(ocds_source, "REQUEST_PAUSE_SECONDS", 0)
    with fake_ocds.FakeOCDS() as fake:
        fake.months[(2026, 11)] = [fake_ocds.release(1000 + i, NOW) for i in range(120)]
        fake.months[(2026, 10)] = []
        run(factory, fake)
        engine = factory.kw["bind"]
        queries = []
        listener = lambda *args: queries.append(1)
        event.listen(engine, "before_cursor_execute", listener)
        try:
            result = run(factory, fake)
        finally:
            event.remove(engine, "before_cursor_execute", listener)
    assert result["updated"] == 120 and len(queries) < 20         # antes: más de 240


def test_summary_labels_carry_names_not_the_filter_counts(tmp_path, monkeypatch, capsys, source):
    import ingest_job
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'base.db'}")
    monkeypatch.setattr(config, "ALL_RUBROS", True)
    real_run = ingest.run_ingestion
    session = ocds_source.session_for(source.base + "/api/v1")
    monkeypatch.setattr(ingest, "run_ingestion", lambda factory, trigger=None: real_run(
        factory, now=NOW, fetch=lambda n, m: ocds_source.fetch_rows(n, m, session=session), months=2, trigger=trigger))
    for _ in range(2):                                             # la 2.ª ya tiene vigentes guardadas
        assert ingest_job.main_with(["--sin-rag"]) == 0
    out = capsys.readouterr().out.split("Ejecución 2")[1]
    assert "- 4111: 1 guardadas" in out and " vigente: " not in out and "· 1 vigente" not in out


def test_priority_line_counts_bases_read_from_pilot_and_requested_rubros(factory, source, monkeypatch):
    import ingest_job
    monkeypatch.setattr(config, "ALL_RUBROS", True)
    run(factory, source)
    line = ingest_job.priority_line(factory, ["201", "202", "203"])
    assert line == "Bases leídas de rubros piloto o solicitados: 2 de 3 · quedan 3 de esos rubros por leer."


# ---------------------------------------------------------------------------
# Base que ya existía (Neon): se agregan las columnas nuevas sin tocar los datos
# ---------------------------------------------------------------------------
def test_existing_runs_table_gets_the_new_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'vieja.db'}"
    old = create_engine(url)
    with old.begin() as conn:      # la tabla como la creó EN-007, con una ejecución guardada
        conn.execute(text("CREATE TABLE actualizaciones (id INTEGER PRIMARY KEY, started_at DATETIME, "
                          "finished_at DATETIME, status VARCHAR(12), trigger VARCHAR(30), months VARCHAR(60), "
                          "source_open_goods INTEGER, source_open_pilot INTEGER, saved_open INTEGER, "
                          "new_count INTEGER, updated_count INTEGER, closed_count INTEGER, error TEXT)"))
        conn.execute(text("INSERT INTO actualizaciones (id, status, saved_open) VALUES (1, 'ok', 188)"))
    old.dispose()
    engine = db.make_engine(url)
    db.create_tables(engine)
    db.create_tables(engine)       # una segunda vez no falla
    columns = {c["name"] for c in inspect(engine).get_columns("actualizaciones")}
    assert {"rubros", "per_rubro"} <= columns and "rubros_solicitados" in inspect(engine).get_table_names()
    with db.make_session_factory(engine)() as session:
        kept = session.get(db.IngestionRun, 1)
        assert kept.saved_open == 188 and kept.per_rubro is None
    engine.dispose()


# ---------------------------------------------------------------------------
# PostgreSQL (opcional): columnas nuevas, conteo guardado y medición de espacio
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("RAG_TEST_POSTGRES_URL"), reason="sin base PostgreSQL de prueba")
def test_postgres_adds_columns_stores_counts_and_measures_space(source):
    import ingest_job
    engine = db.make_engine(os.environ["RAG_TEST_POSTGRES_URL"])
    db.Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS actualizaciones CASCADE"))
        conn.execute(text("CREATE TABLE actualizaciones (id SERIAL PRIMARY KEY, started_at TIMESTAMPTZ, "
                          "finished_at TIMESTAMPTZ, status VARCHAR(12), trigger VARCHAR(30), months VARCHAR(60), "
                          "source_open_goods INTEGER, source_open_pilot INTEGER, saved_open INTEGER, "
                          "new_count INTEGER, updated_count INTEGER, closed_count INTEGER, error TEXT)"))
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    try:
        request(factory, "3011", "4111", "4229")
        result = run(factory, source)
        with factory() as session:
            assert session.get(db.IngestionRun, result["run_id"]).per_rubro["3011"] == 3
        lines = ingest_job.space_lines(engine)
        assert lines[0].startswith("Espacio de las tablas de la app:")
        assert any(line.startswith("- convocatorias:") and "7 filas" in line for line in lines)
    finally:
        db.Base.metadata.drop_all(engine)
        engine.dispose()