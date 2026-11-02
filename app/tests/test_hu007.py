"""Pruebas de pantalla y criterios de aceptación para Convocatorias Vigentes (HU-007).

Criterios de Aceptación:
- Muestra código, objeto, entidad, departamento, fecha de publicación y días desde la publicación.
- El valor estimado aparece como no publicado cuando viene en 0 o None.
- Filtra por rubro piloto, departamento y tipo de procedimiento.
- Aclara que el departamento es el de la entidad compradora, no necesariamente del lugar de entrega.
- Si el bien de la convocatoria está en el catálogo histórico, enlaza a su ficha de precios (del panorama).
- Durante el veredicto, si el código elegido pertenece a un rubro piloto, ofrece ver las convocatorias vigentes de ese rubro sin alterar la sesión de flujo.
"""
from datetime import datetime, timedelta, timezone

import pytest

import config
import fake_ocds
from app import app
from services import db, ingest, ocds_source, tenders

LIMA = ocds_source.LIMA
NOW = datetime(2026, 10, 30, 8, 30, tzinfo=LIMA)


@pytest.fixture
def test_db():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)
    yield factory
    tenders.use_session_factory(None)
    engine.dispose()


@pytest.fixture
def seed_tenders(test_db):
    """Inserta convocatorias vigentes de prueba con diferentes combinaciones."""
    now_utc = NOW.astimezone(timezone.utc)
    with test_db.begin() as session:
        # 1. Ejecución de ingesta exitosa para que availability() sea ok
        run = db.IngestionRun(
            started_at=now_utc - timedelta(hours=2),
            finished_at=now_utc - timedelta(hours=1),
            status="ok",
            months="2026-10",
            source_open_goods=10,
            source_open_pilot=3,
            saved_open=3,
        )
        session.add(run)
        session.flush()

        # 2. Convocatoria 1: Combustibles (1510), La Libertad, Subasta Inversa, con valor estimado
        # Título contiene un bien del catálogo: DIESEL B5 S-50 (CUBSO 233280)
        t1 = db.Tender(
            ocid="ocds-dgv273-seacev3-1001",
            code="1001",
            title="ADQUISICION DE DIESEL B5 S-50 PARA UNIDADES",
            entity="MUNICIPALIDAD PROVINCIAL DE TRUJILLO",
            department="LA LIBERTAD",
            method="Subasta Inversa Electrónica",
            rubros="1510",
            state="vigente",
            published_at=now_utc - timedelta(days=3),
            estimated_value=125000.50,
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        t1.documents.append(
            db.TenderDocument(title="Bases Administrativas", url="https://seace.gob.pe/doc/1001_bases.pdf", is_bases=True)
        )
        session.add(t1)

        # 3. Convocatoria 2: Vehículos (2510), Lima, Licitación Pública, valor estimado en 0 (no publicado)
        # Título contiene: CAMIONETA PICK UP CABINA DOBLE 4 X 4 (CUBSO 65078)
        t2 = db.Tender(
            ocid="ocds-dgv273-seacev3-1002",
            code="1002",
            title="ADQUISICION DE CAMIONETA PICK UP CABINA DOBLE 4 X 4",
            entity="MINISTERIO DEL INTERIOR",
            department="LIMA",
            method="Licitación Pública",
            rubros="2510",
            state="vigente",
            published_at=now_utc - timedelta(days=1),
            estimated_value=0.0,  # Viene en 0 -> debe mostrarse "No publicado"
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        t2.documents.append(
            db.TenderDocument(title="Bases", url="https://seace.gob.pe/doc/1002_bases.pdf", is_bases=True)
        )
        session.add(t2)

        # 4. Convocatoria 3: Bien genérico que no está en el catálogo, valor estimado None
        t3 = db.Tender(
            ocid="ocds-dgv273-seacev3-1003",
            code="1003",
            title="COMPRA DE INSUMOS ESPECIALES DIVERSOS",
            entity="GOBIERNO REGIONAL DE AREQUIPA",
            department="AREQUIPA",
            method="Adjudicación Simplificada",
            rubros="1510",
            state="vigente",
            published_at=now_utc,
            estimated_value=None,
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        session.add(t3)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_tenders_screen_displays_required_fields_and_disclaimers(client, seed_tenders):
    """Criterios:

    - Muestra código, objeto, entidad, departamento, fecha de publicación y días.
    - Aclara que el departamento es el de la entidad compradora, no necesariamente el de entrega.
    - Avisos de actualización y SEACE.
    """
    res = client.get("/convocatorias")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # Campos requeridos en pantalla
    assert "1001" in html
    assert "ADQUISICION DE DIESEL B5 S-50 PARA UNIDADES" in html
    assert "MUNICIPALIDAD PROVINCIAL DE TRUJILLO" in html
    assert "LA LIBERTAD" in html

    # Días desde la publicación
    assert "3 días" in html

    # Aclaración del departamento de la entidad compradora
    assert "El departamento mostrado corresponde a la sede de la entidad compradora convocante" in html

    # Enlace al SEACE
    assert "https://seace.gob.pe/doc/1001_bases.pdf" in html


def test_estimated_value_displayed_as_not_published_when_zero_or_none(client, seed_tenders):
    """Criterio: El valor estimado aparece como no publicado cuando viene en 0 o falta."""
    res = client.get("/convocatorias")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # Para 1001 sí tiene monto publicado: S/ 125,000.50
    assert "125,000.50" in html

    # Para 1002 (valor 0.0) y 1003 (valor None) debe mostrar "No publicado"
    assert "No publicado" in html


def test_filters_by_pilot_rubro_department_and_procurement_method(client, seed_tenders):
    """Criterio: Filtra por rubro piloto, departamento y tipo de procedimiento."""
    # Filtro por departamento: LA LIBERTAD (solo debe aparecer 1001)
    res_dept = client.get("/convocatorias?departamento=LA+LIBERTAD")
    html_dept = res_dept.get_data(as_text=True)
    assert "1001" in html_dept
    assert "1002" not in html_dept

    # Filtro por rubro piloto: 2510 (solo vehículos, 1002)
    res_rubro = client.get("/convocatorias?rubro=2510")
    html_rubro = res_rubro.get_data(as_text=True)
    assert "1002" in html_rubro
    assert "1001" not in html_rubro

    # Filtro por procedimiento: Licitación Pública
    res_method = client.get("/convocatorias?metodo=Licitaci%C3%B3n+P%C3%BAblica")
    html_method = res_method.get_data(as_text=True)
    assert "1002" in html_method
    assert "1001" not in html_method


def test_links_to_historical_catalog_price_sheet_when_good_in_catalog(client, seed_tenders):
    """Criterio: Si el bien de la convocatoria está en el catálogo histórico,

    enlaza a su ficha de precios (/panorama/bien/<codigo>).
    """
    res = client.get("/convocatorias")
    html = res.get_data(as_text=True)

    # 1001 contiene DIESEL B5 S-50 (CUBSO 233280)
    assert "/panorama/bien/233280" in html
    assert "Ver ficha de precios (CUBSO 233280)" in html

    # 1002 contiene CAMIONETA PICK UP (CUBSO 65078)
    assert "/panorama/bien/65078" in html


def test_verdict_offers_open_tenders_for_pilot_rubro_without_altering_session(client):
    """Criterio: Durante el veredicto, si el código elegido pertenece a un rubro piloto,

    ofrece ver las convocatorias vigentes de ese rubro sin alterar la sesión de flujo.
    """
    with client.session_transaction() as sess:
        sess["description"] = "diesel b5 s 50 para maquinaria"
        sess["candidates"] = [233280]
        sess["code"] = 233280

    res = client.post("/veredicto", data={"cost": "12.50", "rnp": "si"})
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # Debe ofrecer ver las convocatorias vigentes del rubro 1510
    assert "Oportunidades de negocio vigentes" in html
    assert "/convocatorias?rubro=1510" in html

    # Verificar que la sesión de flujo se conserva intacta
    with client.session_transaction() as sess:
        assert sess.get("code") == 233280
        assert sess.get("description") == "diesel b5 s 50 para maquinaria"
        assert sess.get("candidates") == [233280]


# ---------------------------------------------------------------------------
# Corrección del 01/11/2026: "Descargar bases" bajaba un ZIP inválido o corrupto
# ---------------------------------------------------------------------------
from services import bases_download  # noqa: E402

PDF = b"%PDF-1.7\n" + b"x" * 20000
RAR = b"Rar!\x1a\x07\x01\x00" + b"r" * 5000
DOCX = b"PK\x03\x04" + b"\x00" * 26 + b"[Content_Types].xml" + b"\x00" * 900 + b"word/document.xml" + b"\x00" * 500
HTML = b"<!DOCTYPE html><html><body>Sesion expirada</body></html>"


class FakeResponse:
    def __init__(self, body, status=200, url="https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=x",
                 headers=None):
        self.body, self.status_code, self.url = body, status, url
        self.headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(body))} | (headers or {})
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self.body), 1000):
            yield self.body[i:i + 1000]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("headers", {}).get("User-Agent")))
        return self.response


@pytest.fixture
def seace(monkeypatch):
    """Sustituye la conexión al SEACE: fake.response es lo que responde."""
    fake = FakeSession(FakeResponse(PDF))
    monkeypatch.setattr(bases_download.requests, "Session", lambda: fake)
    return fake


def test_detects_the_real_format_from_the_first_bytes():
    assert bases_download.detect_kind(PDF) == "pdf"
    assert bases_download.detect_kind(RAR) == "rar"
    assert bases_download.detect_kind(DOCX) == "docx"
    assert bases_download.detect_kind(b"PK\x03\x04" + b"\x00" * 100 + b"bases.pdf") == "zip"
    assert bases_download.detect_kind(HTML) is None and bases_download.detect_kind(b"") is None


def test_only_government_https_links_are_fetched():
    assert bases_download.is_allowed("https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=1")
    assert not bases_download.is_allowed("http://prod1.seace.gob.pe/archivo")          # sin https
    assert not bases_download.is_allowed("https://seace.gob.pe.atacante.com/archivo")
    assert not bases_download.is_allowed("https://169.254.169.254/latest")


def test_screen_links_to_the_app_route_and_keeps_the_original_link(client, seed_tenders):
    html = client.get("/convocatorias").get_data(as_text=True)
    assert "/convocatorias/1001/bases" in html
    assert "https://seace.gob.pe/doc/1001_bases.pdf" in html       # enlace original como respaldo


@pytest.mark.parametrize("body, extension, mimetype", [
    (PDF, "pdf", "application/pdf"),
    (RAR, "rar", "application/vnd.rar"),
    (DOCX, "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
])
def test_download_has_the_real_extension_and_complete_content(client, seed_tenders, seace, body, extension, mimetype):
    seace.response = FakeResponse(body)
    response = client.get("/convocatorias/1001/bases")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == f'attachment; filename="bases_1001.{extension}"'
    assert response.mimetype == mimetype
    assert response.data == body                                    # nada se pierde ni se agrega
    assert seace.calls[0][1] == config.OCDS_USER_AGENT and seace.response.closed


def test_html_or_error_from_seace_is_not_served_as_a_file(client, seed_tenders, seace):
    seace.response = FakeResponse(HTML, headers={"Content-Type": "text/html"})
    response = client.get("/convocatorias/1001/bases")
    assert response.status_code == 502 and "Content-Disposition" not in response.headers
    html = response.get_data(as_text=True)
    assert "No pudimos obtener las bases" in html and "https://seace.gob.pe/doc/1001_bases.pdf" in html
    seace.response = FakeResponse(b"", status=403)
    assert client.get("/convocatorias/1001/bases").status_code == 502


def test_tender_without_bases_or_unknown_code_is_404(client, seed_tenders, seace):
    assert client.get("/convocatorias/1003/bases").status_code == 404
    assert client.get("/convocatorias/9999/bases").status_code == 404
    assert seace.calls == []