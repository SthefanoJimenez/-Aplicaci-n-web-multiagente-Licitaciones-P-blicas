"""Ajustes pedidos por el experto en la entrevista del 11/11/2026.

- Las contrataciones directas y las comparaciones de precios no son postulables: no aparecen como vigentes.
- «Evaluar mi producto» enlaza a las convocatorias vigentes del rubro del bien, con el catálogo completo.
- El panorama ofrece filtrarse por el rubro que la empresa declaró en su perfil.
(La tendencia por meses y el filtro por bien están en tests/test_panorama.py.)
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

import fake_ocds
from app import app
from services import accounts, db, ingest, ocds_source, tenders
from test_hu005_hu006 import CEMENT_I, with_code

LIMA = ocds_source.LIMA
NOW = datetime(2026, 11, 2, 8, 30, tzinfo=LIMA)


@pytest.mark.parametrize("method, expected", [("Contratación Directa", False), ("CONTRATACION DIRECTA", False),
                                              ("Comparación de Precios", False), ("Subasta Inversa Electrónica", True),
                                              ("Licitación Pública Abreviada", True), (None, True)])
def test_procedimientos_postulables(method, expected):
    assert ocds_source.is_postulable(method) is expected
    row = {"method": method, "item_status": "CONVOCADO"}
    assert ocds_source.is_open(row, NOW) is expected


@pytest.fixture
def factory():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)
    yield factory
    tenders.use_session_factory(None)
    engine.dispose()


def test_la_ingesta_no_guarda_directas_ni_comparacion_de_precios(factory, monkeypatch):
    monkeypatch.setattr(ocds_source, "REQUEST_PAUSE_SECONDS", 0)
    releases = [fake_ocds.release(3001, NOW), fake_ocds.release(3002, NOW), fake_ocds.release(3003, NOW)]
    releases[1]["tender"]["procurementMethodDetails"] = "Contratación Directa"
    releases[2]["tender"]["procurementMethodDetails"] = "Comparación de Precios"
    with fake_ocds.FakeOCDS() as fake:
        fake.months[(2026, 11)] = releases
        fake.months[(2026, 10)] = []
        session = ocds_source.session_for(fake.base + "/api/v1")
        ingest.run_ingestion(factory, now=NOW, months=2, trigger="prueba",
                             fetch=lambda when, months: ocds_source.fetch_rows(when, months, session=session))
    assert [t.code for t in tenders.open_tenders()] == ["3001"]


def test_las_guardadas_antes_tampoco_se_muestran(factory):
    """Una contratación directa guardada como vigente antes de este cambio (como 1264631) se oculta
    de inmediato, sin esperar a la siguiente ingesta; el asistente usa la misma consulta."""
    with factory.begin() as session:
        session.add(db.IngestionRun(status="ok", started_at=datetime(2026, 11, 13, 6, tzinfo=LIMA)))
        session.add(db.Tender(ocid="o-1", code="1264631", title="SERVICIO DE AGUA", state="vigente", rubros="1510",
                              method="Contratación Directa", published_at=datetime(2026, 11, 9, tzinfo=LIMA)))
        session.add(db.Tender(ocid="o-2", code="1264740", title="ADQUISICION DE CLORO", state="vigente", rubros="1510",
                              method="Subasta Inversa Electrónica", published_at=datetime(2026, 11, 9, tzinfo=LIMA)))
    assert [t.code for t in tenders.open_tenders()] == ["1264740"]
    html = app.test_client().get("/convocatorias").get_data(as_text=True)
    assert "1264740" in html and "1264631" not in html
    assert "No se muestran las contrataciones directas ni las comparaciones de precios" in html
    assert '<option value="Comparación de Precios"' not in html


def test_evaluar_mi_producto_enlaza_al_rubro_del_bien(signed_in_client, monkeypatch):
    names = {"1510": "1510 · Combustibles y lubricantes", "3011": "3011 · Concreto, cemento y yeso"}
    monkeypatch.setattr(tenders, "rubro_names", lambda: names)
    with_code(signed_in_client, CEMENT_I)
    html = signed_in_client.post("/veredicto", data={"cost": "29.50"}).get_data(as_text=True)
    assert "Este bien corresponde al rubro <strong>Concreto, cemento y yeso</strong>" in html
    assert "/convocatorias?rubro=3011" in html
    # Si el bien no corresponde a ningún rubro del catálogo, no se ofrece el enlace
    monkeypatch.setattr(tenders, "rubro_names", lambda: {"1510": "1510 · Combustibles y lubricantes"})
    html = signed_in_client.post("/veredicto", data={"cost": "29.50"}).get_data(as_text=True)
    assert "Este bien corresponde al rubro" not in html


def test_el_panorama_ofrece_el_rubro_del_perfil(signed_in_client, account_factory):
    html = signed_in_client.get("/panorama").get_data(as_text=True)
    assert "data-my-rubro" not in html                      # sin rubros en el perfil, no hay enlace
    with account_factory() as session:
        uid = session.scalar(select(accounts.User.id).where(accounts.User.email == "flujo@example.test"))
    accounts.save_profile(account_factory, uid, {
        "company_name": "Empresa de prueba", "business_activity": "Venta de combustibles",
        "rubros": "Diésel, gasolina", "monthly_sales": Decimal("10000"), "experience_years": 2,
        "similar_contracts_amount": Decimal("0"), "specialized_staff": ""})
    html = signed_in_client.get("/panorama").get_data(as_text=True)
    assert "Ver el panorama de «Diésel»" in html and "bien=Di%C3%A9sel" in html
    assert "data-my-rubro" not in signed_in_client.get("/panorama?bien=diesel").get_data(as_text=True)