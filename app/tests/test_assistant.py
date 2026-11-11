"""Pruebas automatizadas y evaluación de 10 casos para el Asistente de Postulación (HU-010 / HU-014).

Criterios de Aceptación:
- [x] El asistente usa el perfil guardado, los documentos, el consorcio si existe y los requisitos de las bases.
- [x] Responde con una lista de convocatorias y su semáforo, y explica cada caso citando el requisito y la página de las bases.
- [x] Si falta un dato del perfil (por ejemplo, el rubro o producto que vende), lo pide en lugar de suponerlo.
- [x] Solo menciona convocatorias que existen en la base de datos (con su código real).
- [x] Se evalúa con 10 preguntas de prueba con respuesta esperada.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import app
from services import assistant, db, tenders

NOW = datetime.now(timezone.utc)


@pytest.fixture
def test_db():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)

    with factory.begin() as session:
        # Ejecución de ingesta para que availability() sea ok
        run = db.IngestionRun(
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=1),
            status="ok",
            months="2026-11",
            source_open_goods=5,
            source_open_pilot=2,
            saved_open=2,
        )
        session.add(run)
        session.flush()

        # Convocatoria 1: Combustible en La Libertad (requiere OSINERGMIN y experiencia S/ 150,000)
        t1 = db.Tender(
            ocid="ocds-dgv273-seacev3-2001",
            code="2001",
            title="ADQUISICION DE DIESEL B5 S-50 PARA MAQUINARIA PESADA",
            entity="GOBIERNO REGIONAL DE LA LIBERTAD",
            department="LA LIBERTAD",
            method="Subasta Inversa Electrónica",
            rubros="1510",
            state="vigente",
            published_at=NOW - timedelta(days=2),
            estimated_value=180000.0,
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        t1.documents.append(db.TenderDocument(title="Bases Administrativas", url="https://seace.gob.pe/doc/2001_bases.pdf", is_bases=True))
        # Chunk indexado en RAG
        t1.chunks.append(db.RequirementChunk(
            chunk_index=0,
            page=4,
            text="Requisito: Copia de la constancia de inscripción vigente en el Registro de Hidrocarburos de OSINERGMIN. Experiencia mínima de S/ 150,000 en venta de combustibles.",
            source="pdf",
            file_name="bases_2001.pdf",
            embedding=[0.1] * 768,
        ))
        session.add(t1)

        # Convocatoria 2: Vehículos en Lima (pide facturación S/ 200,000)
        t2 = db.Tender(
            ocid="ocds-dgv273-seacev3-2002",
            code="2002",
            title="ADQUISICION DE CAMIONETAS 4X4 DOBLE CABINA",
            entity="MINISTERIO DE SALUD",
            department="LIMA",
            method="Licitación Pública",
            rubros="2510",
            state="vigente",
            published_at=NOW - timedelta(days=1),
            estimated_value=350000.0,
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        t2.documents.append(db.TenderDocument(title="Bases", url="https://seace.gob.pe/doc/2002_bases.pdf", is_bases=True))
        t2.chunks.append(db.RequirementChunk(
            chunk_index=0,
            page=7,
            text="Experiencia del postor en la especialidad: acreditación de facturación acumulada por S/ 200,000.00 en venta de vehículos similares.",
            source="pdf",
            file_name="bases_2002.pdf",
            embedding=[0.2] * 768,
        ))
        session.add(t2)

    yield factory
    tenders.use_session_factory(None)
    engine.dispose()


@pytest.fixture
def client(signed_in_client):
    """Pantalla /asistente: desde HU-008 exige una cuenta con sesión iniciada."""
    app.config["TESTING"] = True
    return signed_in_client


# ---------------------------------------------------------------------------
# 10 CASOS DE PRUEBA Y EVALUACIÓN CON RESPUESTA ESPERADA
# ---------------------------------------------------------------------------

def test_case_01_missing_rubro_requests_data_instead_of_assuming(test_db):
    """Caso 1: El usuario pregunta genéricamente sin decir qué vende.
    Esperado: El asistente NO supone el rubro; pide qué bienes vende su empresa.
    """
    res = assistant.ask_assistant("Hola, soy nuevo y quiero saber a qué licitaciones puedo postular hoy.")
    assert res["status"] == "need_data"
    assert "qué bienes o productos vende" in res["response"]
    assert "rubro" in res["response"].lower()


def test_case_02_fuel_vendor_without_sales_to_state_and_no_consortium(test_db):
    """Caso 2: Proveedor de combustible sin ventas al Estado y sin consorcio.
    Esperado: Alerta de semáforo ROJO citando las Bases de 2001 (pág. 4) por falta de experiencia previa.
    """
    query = "Vendo combustible diésel, pero nunca le he vendido al Estado y no tengo experiencia previa."
    res = assistant.ask_assistant(query)
    assert res["status"] != "need_data"
    assert "2001" in res["response"]
    assert "pág. 4" in res["response"] or "página 4" in res["response"].lower()
    assert "ROJO" in res["response"] or "🔴" in res["response"]
    assert "facturación" in res["response"].lower() or "experiencia" in res["response"].lower()


def test_case_03_fuel_vendor_in_consortium_with_experienced_partner(test_db):
    """Caso 3: Proveedor de combustible que nunca vendió al Estado pero postula EN CONSORCIO con socio con experiencia.
    Esperado: Semáforo AMARILLO (viable en consorcio aportando experiencia del socio) citando Bases pág. 4.
    """
    query = "Vendo combustible diésel, no he vendido al Estado pero quiero postular en consorcio con un socio que tiene experiencia y factura."
    res = assistant.ask_assistant(query)
    assert "2001" in res["response"]
    assert "AMARILLO" in res["response"] or "🟡" in res["response"]
    assert "CONSORCIO" in res["response"]
    assert "Bases" in res["response"]


def test_case_04_fuel_vendor_missing_osinergmin_is_red(test_db):
    """Caso 4: Proveedor de combustible sin OSINERGMIN.
    Esperado: Semáforo ROJO por falta de habilitación legal indispensable de hidrocarburos.
    """
    query = "Vendo combustible diésel, tengo facturación de 200 mil soles pero no tengo OSINERGMIN."
    res = assistant.ask_assistant(query)
    assert "2001" in res["response"]
    assert "ROJO" in res["response"] or "🔴" in res["response"]
    assert "OSINERGMIN" in res["response"] or "Hidrocarburos" in res["response"]


def test_case_05_fuel_vendor_with_all_requirements_is_green(test_db):
    """Caso 5: Proveedor de combustible con OSINERGMIN, RNP y facturación suficiente (S/ 200,000 > 150,000).
    Esperado: Semáforo VERDE (postulación recomendada) con cita a bases.
    """
    query = "Vendo diésel y combustible, tengo RNP vigente, registro de OSINERGMIN y facturación acumulada de S/ 200,000."
    res = assistant.ask_assistant(query)
    assert "2001" in res["response"]
    assert "VERDE" in res["response"] or "🟢" in res["response"]
    assert "pág. 4" in res["response"] or "Bases" in res["response"]


def test_case_06_vehicle_vendor_with_high_experience_is_green(test_db):
    """Caso 6: Proveedor de vehículos con facturación privada de S/ 300,000 (supera S/ 200,000 de la base 2002).
    Esperado: Semáforo VERDE citando la pág. 7 de las bases de la licitación 2002.
    """
    query = "Tengo una empresa de venta de vehículos y camionetas, con RNP y ventas privadas por S/ 300,000."
    res = assistant.ask_assistant(query)
    assert "2002" in res["response"]
    assert "pág. 7" in res["response"] or "página 7" in res["response"].lower()
    assert "VERDE" in res["response"] or "🟢" in res["response"]


def test_case_07_vehicle_vendor_without_experience_is_red(test_db):
    """Caso 7: Proveedor de vehículos nuevo sin ventas previas.
    Esperado: Semáforo ROJO para la licitación 2002 citando la exigencia de S/ 200,000 en pág. 7.
    """
    query = "Vendo camionetas y vehículos, tengo RNP pero sin experiencia previa y sin consorcio."
    res = assistant.ask_assistant(query)
    assert "2002" in res["response"]
    assert "ROJO" in res["response"] or "🔴" in res["response"]
    assert "200,000" in res["response"] or "facturación" in res["response"].lower()


def test_case_08_no_rnp_triggers_red_admissibility_alert(test_db):
    """Caso 8: Proveedor que indica explícitamente no tener RNP.
    Esperado: Alerta inmediata de descalificación insubsanable por falta de RNP.
    """
    query = "Vendo camionetas, tengo experiencia de 500 mil pero no tengo RNP."
    res = assistant.ask_assistant(query)
    assert "ROJO" in res["response"] or "🔴" in res["response"]
    assert "RNP" in res["response"] or "Registro Nacional de Proveedores" in res["response"]


def test_case_09_query_only_mentions_real_tenders_in_database(test_db):
    """Caso 9: Verificar que el asistente solo cite convocatorias reales existentes (2001, 2002) y no invente códigos.
    """
    query = "Vendo combustible diésel y camionetas, ¿a qué puedo presentarme?"
    res = assistant.ask_assistant(query)
    response_text = res["response"]
    # Debe mencionar los códigos reales
    assert "2001" in response_text or "2002" in response_text
    # No debe contener códigos falsos como 9999, 1234, etc.
    assert "9999" not in response_text


def test_case_10_multiturn_conversation_maintains_profile_and_history(test_db, client):
    """Caso 10: Conversación multi-turno en la pantalla web (/asistente).
    Turno 1: Usuario no da rubro -> el sistema pide el rubro.
    Turno 2: Usuario da rubro de combustible y RNP -> el sistema evalúa y conserva el perfil en sesión.
    """
    # Turno 1
    r1 = client.post("/asistente", data={"query": "¿Puedo postular hoy a alguna licitación?"}, follow_redirects=True)
    html1 = r1.get_data(as_text=True)
    assert "qué bienes o productos vende tu empresa" in html1

    # Turno 2
    r2 = client.post("/asistente", data={"query": "Mi empresa vende combustible diésel, tengo RNP y OSINERGMIN pero nunca le he vendido al Estado"}, follow_redirects=True)
    html2 = r2.get_data(as_text=True)
    assert "2001" in html2
    assert "Combustibles y lubricantes" in html2
    assert "Perfil del Proveedor" in html2


def test_case_11_assistant_forms_send_the_session_token(client):
    """Caso 11 (regresión): los dos formularios de la pantalla deben llevar el token de la sesión.

    Sin el campo oculto, el navegador recibía 400 «El formulario venció» al pulsar
    «Consultar» o «Reiniciar chat»; las pruebas no lo detectaban porque el cliente de
    pruebas añade el token por su cuenta.
    """
    html = client.get("/asistente").get_data(as_text=True)
    assert 'class="chat-form"' in html
    assert html.count('name="csrf_token"') >= 2


def test_case_12_markdown_of_the_answer_is_rendered_as_html():
    """Caso 12 (regresión): la respuesta se mostraba con los asteriscos y los «>» a la vista."""
    from services.formatting import markdown

    rendered = markdown("### Título\n\n- **Entidad:** Municipalidad\n> aviso `DATABASE_URL`\n---")
    assert "<h3>Título</h3>" in rendered
    assert "<li><strong>Entidad:</strong> Municipalidad</li>" in rendered
    assert "<blockquote>" in rendered and "<code>DATABASE_URL</code>" in rendered
    assert "<hr>" in rendered
    # El texto del usuario nunca puede convertirse en etiquetas
    assert "<script>" not in markdown("<script>alert(1)</script>")


def test_case_13_negated_sales_are_not_read_as_previous_sales():
    """Caso 13 (regresión): «nunca le he vendido al Estado» se leía como proveedor habitual."""
    assert assistant.extract_profile_updates(
        "Vendo combustible diésel, nunca le he vendido al Estado", {})["sold_to_state"] is False
    assert assistant.extract_profile_updates(
        "He vendido al Estado durante cinco años", {})["sold_to_state"] is True
