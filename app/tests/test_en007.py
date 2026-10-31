"""EN-007: contrato de datos, cliente OCDS, modelo, ingesta diaria y lectura desde la app.

Usan la API simulada (tests/fake_ocds.py) y SQLite: no necesitan internet ni Neon.
"""
import gzip
import io
import json
import zipfile
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest
from flask import render_template_string
from sqlalchemy import inspect

import config
import fake_ocds
from app import app
from services import db, ingest, ocds_source, tender_contract, tenders

LIMA = ocds_source.LIMA
NOW = datetime(2026, 10, 30, 8, 30, tzinfo=LIMA)


# ---------------------------------------------------------------------------
# Preparación
# ---------------------------------------------------------------------------
@pytest.fixture
def factory():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    yield db.make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(ocds_source, "REQUEST_PAUSE_SECONDS", 0)
    with fake_ocds.FakeOCDS() as fake:
        r = fake_ocds.release
        fake.months[(2026, 10)] = [
            r(101, NOW, family="15101505"),                       
            r(102, NOW, family="25101500"),                       
            r(103, NOW, family="44121701"),                       
            r(104, NOW, family="15101505", item_status="ADJUDICADO"),   
            r(105, NOW, family="15101505", category="services"),        
        ]
        fake.months[(2026, 9)] = [r(106, NOW, family="25101500", date=NOW - timedelta(days=40))]
        yield fake


def fetch_from(fake):
    session = ocds_source.session_for(fake.base + "/api/v1")
    return lambda now, months: ocds_source.fetch_rows(now, months, session=session)


def run(factory, fake, now=NOW):
    return ingest.run_ingestion(factory, now=now, fetch=fetch_from(fake), months=2, trigger="prueba")


# ---------------------------------------------------------------------------
# Contrato de datos
# ---------------------------------------------------------------------------
def test_contract_example_matches_the_entities_and_has_no_user_data():
    example = tender_contract.example()
    tender = example["tender"]
    assert set(tender) == {f.name for f in fields(tender_contract.Tender)}
    assert set(tender["documents"][0]) == {f.name for f in fields(tender_contract.TenderDocument)}
    assert set(tender["requirements"][0]) == {f.name for f in fields(tender_contract.Requirement)}
    assert set(example["ingestion_run"]) == {f.name for f in fields(tender_contract.IngestionRun)}
    assert tender["state"] in tender_contract.TENDER_STATES
    assert tender["requirements_status"] in tender_contract.REQUIREMENTS_STATUSES
    names = {name for name, _, _ in tender_contract.field_table(tender_contract.Tender)}
    assert not names & {"cost", "costo", "profile", "perfil", "user", "usuario"}   # RN-001
    assert ("estimated_value", "Optional[float]", True) in tender_contract.field_table(tender_contract.Tender)


def test_tables_follow_the_contract_with_keys_and_no_user_table(factory):
    inspector = inspect(factory.kw["bind"])
    assert set(inspector.get_table_names()) == {"convocatorias", "documentos", "requisitos", "actualizaciones"}
    assert inspector.get_foreign_keys("documentos")[0]["referred_table"] == "convocatorias"
    assert {fk["referred_table"] for fk in inspector.get_foreign_keys("requisitos")} == {"convocatorias", "documentos"}
    columns = {c["name"] for c in inspector.get_columns("convocatorias")}
    contract = {f.name for f in fields(tender_contract.Tender)} - {"documents", "requirements"}
    assert contract <= columns


# ---------------------------------------------------------------------------
# Cliente OCDS (funciones del SP-005)
# ---------------------------------------------------------------------------
def test_reads_zip_gzip_and_jsonl(tmp_path):
    package = {"releases": [{"ocid": "a", "date": "2026-10-01"}]}
    (tmp_path / "plano.json").write_text(json.dumps(package))
    (tmp_path / "comprimido.gz").write_bytes(gzip.compress(json.dumps(package).encode()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("lineas.jsonl", json.dumps(package) + "\n" + json.dumps({"releases": [{"ocid": "b"}]}))
    (tmp_path / "zip.bin").write_bytes(buffer.getvalue())
    for name, expected in [("plano.json", 1), ("comprimido.gz", 1), ("zip.bin", 2)]:
        assert len(list(ocds_source.read_ocds_file(tmp_path / name))) == expected
    (tmp_path / "roto.bin").write_bytes(b"PK\x03\x04no es un zip")
    with pytest.raises(ocds_source.SourceError):
        list(ocds_source.read_ocds_file(tmp_path / "roto.bin"))


def test_merges_releases_of_the_same_process_keeping_the_latest():
    older = {"ocid": "x", "date": "2026-10-01", "tender": {"title": "viejo"}}
    newer = {"ocid": "x", "date": "2026-10-05", "tender": {"title": "nuevo"}}
    merged = list(ocds_source.compiled_releases([{"releases": [newer, older]}]))
    assert len(merged) == 1 and merged[0]["tender"]["title"] == "nuevo"


def test_open_rule_rubro_and_bases_priority():
    row = ocds_source.normalize_release(fake_ocds.release(7, NOW, family="15101505"))
    assert ocds_source.is_goods(row) and ocds_source.is_open(row, NOW)
    assert row["deadline"] is None            # tenderPeriod de un día: no es fecha límite
    assert row["value"] is None               # 0 = no publicado
    assert ocds_source.rubros_of(row) == ["1510"]
    assert ocds_source.bases_document(row)["title"] == "Bases Administrativas"
    row["documents"].append({"title": "Bases Integradas", "type": "biddingDocuments", "url": "u"})
    assert ocds_source.bases_document(row)["title"] == "Bases Integradas"
    closed = ocds_source.normalize_release(fake_ocds.release(8, NOW, item_status="ADJUDICADO"))
    assert not ocds_source.is_open(closed, NOW)


def test_months_cross_the_year_and_client_name_is_configurable(monkeypatch):
    assert ocds_source.months_to_fetch(datetime(2027, 1, 5, tzinfo=LIMA), 2) == [(2027, 1), (2026, 12)]
    assert ocds_source.session_for().headers["User-Agent"] == config.OCDS_USER_AGENT
    assert config.OCDS_USER_AGENT.startswith("Mozilla/5.0")
    assert ocds_source.session_for(user_agent="Prueba/1.0").headers["User-Agent"] == "Prueba/1.0"


def test_database_url_from_neon_uses_psycopg():
    assert db.engine_url("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert db.engine_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert db.engine_url("sqlite:///x.db") == "sqlite:///x.db"


# ---------------------------------------------------------------------------
# Ingesta
# ---------------------------------------------------------------------------
def test_first_run_saves_only_open_goods_of_the_pilot_rubros(factory, source):
    result = run(factory, source)
    assert result["status"] == "ok"
    assert (result["source_open_pilot"], result["saved_open"], result["new"]) == (3, 3, 3)
    with factory() as session:
        saved = {t.code: t for t in session.query(db.Tender)}
        assert set(saved) == {"101", "102", "106"}
        tender = saved["101"]
        assert tender.state == "vigente" and tender.rubros == "1510" and tender.estimated_value is None
        assert ingest.as_utc(tender.first_seen_at) == NOW.astimezone(timezone.utc)
        bases = [d for d in tender.documents if d.is_bases]
        assert len(tender.documents) == 2 and bases[0].title == "Bases Administrativas"
        update = session.get(db.IngestionRun, result["run_id"])
        assert update.status == "ok" and update.months == "2026-10,2026-09" and update.trigger == "prueba"
        assert (update.source_open_goods, update.source_open_pilot, update.saved_open) == (4, 3, 3)


def test_running_twice_does_not_duplicate_and_keeps_first_seen(factory, source):
    run(factory, source)
    later = NOW + timedelta(days=1)
    result = run(factory, source, now=later)
    assert (result["new"], result["updated"], result["closed"], result["documents_added"]) == (0, 3, 0, 0)
    with factory() as session:
        assert session.query(db.Tender).count() == 3 and session.query(db.TenderDocument).count() == 6
        tender = session.query(db.Tender).filter_by(code="101").one()
        assert ingest.as_utc(tender.first_seen_at) == NOW.astimezone(timezone.utc)
        assert ingest.as_utc(tender.last_seen_at) == later.astimezone(timezone.utc)


def test_tender_that_is_no_longer_open_changes_state_and_is_not_deleted(factory, source):
    run(factory, source)
    source.months[(2026, 10)][0] = fake_ocds.release(101, NOW, item_status="ADJUDICADO")
    result = run(factory, source, now=NOW + timedelta(days=1))
    assert result["closed"] == 1 and result["saved_open"] == 2
    with factory() as session:
        tender = session.query(db.Tender).filter_by(code="101").one()
        assert tender.state == "no_vigente" and tender.closed_at is not None
    source.months[(2026, 10)][0] = fake_ocds.release(101, NOW)            # vuelve a estar vigente
    run(factory, source, now=NOW + timedelta(days=2))
    with factory() as session:
        tender = session.query(db.Tender).filter_by(code="101").one()
        assert tender.state == "vigente" and tender.closed_at is None


def test_source_failure_keeps_previous_data_and_records_a_failed_run(factory, source):
    run(factory, source)
    source.fail = 403
    result = run(factory, source, now=NOW + timedelta(days=1))
    assert result["status"] == "fallida" and result["http_status"] == 403
    with factory() as session:
        assert session.query(db.Tender).filter_by(state="vigente").count() == 3
        failed = session.get(db.IngestionRun, result["run_id"])
        assert failed.status == "fallida" and "403" in failed.error and failed.finished_at is not None


def test_error_halfway_rolls_back_everything(factory, source, monkeypatch):
    run(factory, source)
    source.months[(2026, 10)].append(fake_ocds.release(107, NOW))
    calls = []

    def broken(tender, row):
        calls.append(row["code"])
        if len(calls) == 3:
            raise RuntimeError("se cortó la conexión")
        return 0

    monkeypatch.setattr(ingest, "_sync_documents", broken)
    result = run(factory, source, now=NOW + timedelta(days=1))
    assert result["status"] == "fallida" and "se cortó" in result["error"]
    with factory() as session:
        assert session.query(db.Tender).filter_by(code="107").count() == 0       # nada a medias
        assert all(ingest.as_utc(t.last_seen_at) == NOW.astimezone(timezone.utc) for t in session.query(db.Tender))


def test_unidentified_clients_get_403_like_the_real_portal(source):
    session = ocds_source.session_for(source.base, user_agent="python-requests/2.34")
    with pytest.raises(ocds_source.SourceError) as error:
        ocds_source.fetch_rows(NOW, 1, session=session)
    assert error.value.status == 403


# ---------------------------------------------------------------------------
# Tarea de GitHub Actions
# ---------------------------------------------------------------------------
def test_job_without_database_url_does_nothing(monkeypatch, capsys):
    import ingest_job
    monkeypatch.setattr(config, "DATABASE_URL", None)
    assert ingest_job.main() == 2 and "Falta DATABASE_URL" in capsys.readouterr().out


def test_job_writes_summary_and_never_prints_the_password(tmp_path, monkeypatch, capsys, source):
    import ingest_job
    url = f"sqlite:///{tmp_path / 'base.db'}"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "resumen.md"))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    real_run = ingest.run_ingestion
    monkeypatch.setattr(ingest, "run_ingestion",
                        lambda factory, trigger=None: real_run(factory, now=NOW, fetch=fetch_from(source), months=2,
                                                               trigger=trigger))
    assert ingest_job.main() == 0
    assert "Vigentes guardadas: 3" in (tmp_path / "resumen.md").read_text(encoding="utf-8")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://usuario:clave-secreta@host/db")
    monkeypatch.setattr(db, "make_engine", lambda url=None: db.create_engine(url or "sqlite://"))
    monkeypatch.setattr(ingest, "run_ingestion", lambda factory, trigger=None: {
        "run_id": 1, "status": "fallida", "error": "no conecta a usuario:clave-secreta@host", "http_status": None})
    assert ingest_job.main() == 1
    assert "clave-secreta" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Lectura desde la app y aviso de RN-002
# ---------------------------------------------------------------------------
@pytest.fixture
def app_reads(factory, monkeypatch):
    tenders.use_session_factory(factory)
    yield factory
    tenders.use_session_factory(None)


def test_app_reads_open_tenders_as_contract_objects(app_reads, source):
    run(app_reads, source)
    result = tenders.open_tenders()
    assert [t.code for t in result] == ["101", "102", "106"]   # de la más reciente a la más antigua
    assert isinstance(result[0], tender_contract.Tender) and result[0].documents[0].url.startswith("https://")
    assert [t.code for t in tenders.open_tenders(rubro="2510")] == ["102", "106"]
    assert tenders.tender_by_code("101").entity == "ENTIDAD 101" and tenders.tender_by_code("999") is None
    assert tender_contract.tender_to_dict(result[0])["first_seen_at"].startswith("2026-10-30T13:30")


def test_availability_states(app_reads, source, monkeypatch):
    state = tenders.availability(now=NOW)
    assert state["ok"] and state["last_update"] is None            # base vacía: todavía sin carga
    run(app_reads, source)
    state = tenders.availability(now=NOW + timedelta(hours=1))
    assert state["ok"] and not state["stale"] and state["last_update_text"].startswith("30 oct 2026")
    assert tenders.availability(now=NOW + timedelta(days=2))["stale"]
    source.fail = 500
    run(app_reads, source, now=NOW + timedelta(days=1))
    assert tenders.availability(now=NOW + timedelta(days=1, hours=1))["last_failed"]
    tenders.use_session_factory(None)
    monkeypatch.setattr(config, "DATABASE_URL", None)
    assert tenders.availability() == {"ok": False, "reason": "sin_configurar", "last_update": None,
                                      "last_update_text": None, "stale": False, "last_failed": False}
    with pytest.raises(tenders.TendersUnavailable):
        tenders.open_tenders()


def test_status_route_and_notice(app_reads, source, monkeypatch):
    client = app.test_client()
    run(app_reads, source)
    body = client.get("/convocatorias/estado").get_json()
    assert body["ok"] and body["open_tenders"] == 3
    with app.test_request_context():
        ok_html = render_template_string('{% include "_tenders_notice.html" %}', tenders_state=tenders.availability())
        assert "Actualizado el" in ok_html
        down = {"ok": False, "reason": "sin_conexion"}
        down_html = render_template_string('{% include "_tenders_notice.html" %}', tenders_state=down)
        assert "No pudimos consultar las convocatorias" in down_html and "datos históricos" in down_html
    tenders.use_session_factory(None)
    monkeypatch.setattr(config, "DATABASE_URL", None)
    response = client.get("/convocatorias/estado")
    assert response.status_code == 503 and response.get_json()["reason"] == "sin_configurar"