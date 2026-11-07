
from datetime import datetime, timedelta

import pytest

import config
import fake_ocds
from app import app
from services import db, ingest, ocds_source, rubros, tenders

LIMA = ocds_source.LIMA
NOW = datetime(2026, 11, 6, 8, 30, tzinfo=LIMA)


def good(n, unspsc, description, item_status="CONVOCADO", days_ago=2, category="goods"):
    """Convocatoria con la forma real: código CUBSO y, aparte, la clasificación UNSPSC con su descripción."""
    row = fake_ocds.release(n, NOW, item_status=item_status, category=category, date=NOW - timedelta(days=days_ago))
    row["tender"]["items"] = [{
        "description": f"ADQUISICION DE BIENES {n}", "statusDetails": item_status,
        "classification": {"scheme": "CUBSO", "id": unspsc + "00123456", "description": f"PRODUCTO CUBSO {n}"},
        "additionalClassifications": [{"scheme": "UNSPSC", "id": unspsc, "description": description}],
    }]
    return row


@pytest.fixture
def factory():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)
    yield factory
    tenders.use_session_factory(None)
    engine.dispose()


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(ocds_source, "REQUEST_PAUSE_SECONDS", 0)
    with fake_ocds.FakeOCDS() as fake:
        fake.months[(2026, 11)] = [
            good(301, "15101505", "Combustible diésel"),                        # 1510 piloto
            good(302, "30111601", "Cemento"),                                   # 3011
            good(303, "30111601", "Cemento"),
            good(304, "30111504", "Arena"),
            good(305, "30111601", "Cemento", item_status="ADJUDICADO"),          # 3011, ya no vigente
            good(306, "41111712", "Microscopios invertidos"),                   # 4111
            good(307, "41111712", "Microscopios invertidos", category="services"),   # servicio: no cuenta
        ]
        fake.months[(2026, 10)] = [
            good(308, "42295513", "Stents coronarios", days_ago=20),            # 4229, dentro del mes
            good(309, "53102710", "Uniformes", days_ago=45, item_status="ADJUDICADO"),   # fuera del mes
        ]
        yield fake


def run(factory, fake):
    session = ocds_source.session_for(fake.base + "/api/v1")
    return ingest.run_ingestion(factory, now=NOW, fetch=lambda n, m: ocds_source.fetch_rows(n, m, session=session),
                                months=2, trigger="prueba")


@pytest.fixture
def client(account_client):
    return account_client


# ---------------------------------------------------------------------------
# 1. Catálogo a partir de las convocatorias de bienes del último mes
# ---------------------------------------------------------------------------
def test_catalog_counts_goods_tenders_of_the_last_month_by_unspsc_family(source):
    session = ocds_source.session_for(source.base + "/api/v1")
    rows = ocds_source.fetch_rows(NOW, 2, session=session)[0]
    entries = rubros.catalog_from_rows(rows, NOW)
    assert entries == [
        ("3011", "Ej.: Cemento, arena", 4),         # cuenta también la adjudicada: es del último mes
        ("1510", "Combustibles y lubricantes", 1),   # piloto: nombre fijo
        ("4111", "Ej.: Microscopios invertidos", 1),  # el servicio 307 no cuenta
        ("4229", "Ej.: Stents coronarios", 1),
    ]                                                 # 5310 se publicó hace 45 días: queda fuera


def test_catalog_name_never_uses_the_item_description():
    row = {"ocid": "x", "category": "goods", "published": NOW.isoformat(),
           "items": [{"description": "ADQUISICION DE UNIFORMES",
                      "classifications": [{"scheme": "UNSPSC", "id": "53102710",
                                           "description": "ADQUISICION DE UNIFORMES"}]}]}
    assert rubros.catalog_from_rows([row], NOW) == [("5310", None, 1)]


def test_ingestion_rebuilds_the_catalog_and_keeps_it_if_the_source_is_empty(factory, source):
    result = run(factory, source)
    assert result["catalog"] == 4
    with factory() as session:
        assert [(r.code, r.tenders_month) for r in rubros.catalog(session)] == [
            ("3011", 4), ("1510", 1), ("4111", 1), ("4229", 1)]
    source.months[(2026, 11)], source.months[(2026, 10)] = [], []
    run(factory, source)
    with factory() as session:
        assert len(rubros.catalog(session)) == 4          # no se borra el catálogo por una fuente vacía


# ---------------------------------------------------------------------------
# 2. El filtro muestra el catálogo completo
# ---------------------------------------------------------------------------
def test_filter_lists_the_whole_catalog_not_only_fuel_and_vehicles(factory, source, client):
    run(factory, source)
    page = client.get("/convocatorias").get_data(as_text=True)
    for label in ("1510 · Combustibles y lubricantes · 1 vigente", "2510 · Vehículos de motor · sin datos",
                  "3011 · Ej.: Cemento, arena · sin datos", "4111 · Ej.: Microscopios invertidos · sin datos",
                  "4229 · Ej.: Stents coronarios · sin datos"):
        assert label in page


def test_catalog_page_lists_every_rubro_with_its_action(factory, source, client):
    run(factory, source)
    page = client.get("/rubros").get_data(as_text=True)
    assert "5 rubros" in page                                      # 4 del catálogo + 2510 piloto sin datos
    assert "Ver convocatorias" in page and page.count("Solicitar este rubro") == 3   # 3011, 4111, 4229
    found = client.get("/rubros?q=cemento").get_data(as_text=True)
    assert "1 rubro<" in found and "3011" in found and "4111" not in found


# ---------------------------------------------------------------------------
# 3. «Solicitar este rubro» cuando no hay datos
# ---------------------------------------------------------------------------
def test_rubro_without_data_offers_the_request_button_and_one_with_data_does_not(factory, source, client):
    run(factory, source)
    page = client.get("/convocatorias?rubro=3011").get_data(as_text=True)
    assert "aún no tiene convocatorias cargadas" in page and "Solicitar este rubro" in page
    assert 'name="csrf_token"' in page
    assert "En el último mes se publicaron 4 convocatorias" in page
    page = client.get("/convocatorias?rubro=1510").get_data(as_text=True)
    assert "Solicitar este rubro" not in page and "ADQUISICION 301" in page


def test_request_is_saved_and_the_rubro_appears_after_the_next_ingestion(factory, source, client):
    run(factory, source)
    response = client.post("/convocatorias/solicitar-rubro", data={"rubro": "3011", "empresa": "Ferretería Andina SAC"})
    assert response.status_code == 303 and response.headers["Location"].endswith("rubro=3011&solicitud=ok")
    with factory() as session:
        assert rubros.requests_table(session)[0][:3] == ("3011", None, "Ferretería Andina SAC")
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Solicitud registrada para el rubro 3011" in page and "Solicitar este rubro" not in page
    page = client.get("/convocatorias?rubro=3011").get_data(as_text=True)
    assert "ya fue solicitado" in page                              # aún no corre la ingesta

    run(factory, source)                                            # la siguiente actualización diaria
    page = client.get("/convocatorias?rubro=3011").get_data(as_text=True)
    assert all(f"ADQUISICION {n}" in page for n in (302, 303, 304)) and "ADQUISICION 305" not in page
    assert "Solicitar este rubro" not in page and "ya fue solicitado" not in page
    assert "3011 · Ej.: Cemento, arena · 3 vigentes" in page


def test_requesting_twice_keeps_one_request(factory, source, client):
    run(factory, source)
    for _ in range(2):
        client.post("/convocatorias/solicitar-rubro", data={"rubro": "4111"})
    with factory() as session:
        assert rubros.requested_codes(session) == ["4111"] and len(rubros.requests_table(session)) == 1


def test_catalog_page_button_saves_the_request(factory, source, client):
    run(factory, source)
    assert 'name="csrf_token"' in client.get("/rubros").get_data(as_text=True)
    response = client.post("/convocatorias/solicitar-rubro", data={"rubro": "4229"}, follow_redirects=True)
    assert "Solicitud registrada para el rubro 4229" in response.get_data(as_text=True)
    page = client.get("/rubros").get_data(as_text=True)
    assert "Solicitado" in page and page.count("Solicitar este rubro") == 2


def test_invalid_request_is_rejected_and_nothing_is_saved(factory, source, client):
    run(factory, source)
    for bad in ("abc", "12", ""):
        response = client.post("/convocatorias/solicitar-rubro", data={"rubro": bad}, follow_redirects=True)
        assert "No se pudo registrar la solicitud" in response.get_data(as_text=True)
    with factory() as session:
        assert rubros.requests_table(session) == []


def test_request_when_the_database_is_down_says_so(factory, source, client, monkeypatch):
    run(factory, source)
    def down(code, company=""):
        raise tenders.TendersUnavailable("sin_conexion")
    monkeypatch.setattr(tenders, "request_rubro", down)
    response = client.post("/convocatorias/solicitar-rubro", data={"rubro": "3011"})
    assert response.headers["Location"].endswith("solicitud=sin_conexion")


def test_request_is_pending_until_an_ingestion_downloads_it(factory, source):
    run(factory, source)
    with factory.begin() as session:
        rubros.request_rubro(session, "4111")
        assert rubros.status(session, "4111")["pending"] is True
    run(factory, source)
    with factory.begin() as session:
        assert rubros.status(session, "4111")["pending"] is False
        assert rubros.status(session, "4111")["can_request"] is False      # ya tiene vigentes cargadas
        rubros.withdraw_rubro(session, "4111")
    run(factory, source)                                                  # ya no se descarga
    with factory.begin() as session:
        assert rubros.status(session, "4111")["can_request"] is True
        rubros.request_rubro(session, "4111")                              # se vuelve a pedir
        assert rubros.status(session, "4111")["pending"] is True


def test_in_all_rubros_mode_a_request_is_never_pending(factory, source, monkeypatch):
    monkeypatch.setattr(config, "ALL_RUBROS", True)
    run(factory, source)
    with factory.begin() as session:
        rubros.request_rubro(session, "4229")
        info = rubros.status(session, "4229")
    assert info["pending"] is False and info["open_count"] == 1           # ya estaba cargado


def test_catalog_command_prints_the_count_per_family(tmp_path, monkeypatch, capsys, source):
    import ingest_job
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'base.db'}")
    assert ingest_job.main_with(["--catalogo"]) == 1                     # todavía no hay catálogo
    assert "aún no se generó" in capsys.readouterr().out
    real_run = ingest.run_ingestion
    session = ocds_source.session_for(source.base + "/api/v1")
    monkeypatch.setattr(ingest, "run_ingestion", lambda factory, trigger=None: real_run(
        factory, now=NOW, fetch=lambda n, m: ocds_source.fetch_rows(n, m, session=session), months=2, trigger=trigger))
    assert ingest_job.main_with(["--sin-rag"]) == 0
    assert "Catálogo de rubros: 4 rubros con convocatorias de bienes en los últimos 30 días" in capsys.readouterr().out
    assert ingest_job.main_with(["--catalogo"]) == 0
    out = capsys.readouterr().out
    assert "Catálogo de rubros: 4 rubros · 7 convocatorias de bienes en los últimos 30 días" in out
    assert "- 3011 · 4 · 0 · sin datos · Ej.: Cemento, arena" in out
    assert "- 1510 · 1 · 1 · piloto · Combustibles y lubricantes" in out
