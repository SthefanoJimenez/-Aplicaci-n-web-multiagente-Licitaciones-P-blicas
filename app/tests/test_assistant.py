"""HU-014: asistente de postulación («¿a qué convocatorias puedo postular?»).

Las 10 preguntas de prueba están al final, cada una con su respuesta esperada. El asistente
trabaja con el perfil guardado, los documentos de la empresa, la etapa y el cronograma de cada
convocatoria y los requisitos leídos de las bases. Cuando no puede verificar algo, lo dice
(«por verificar») en lugar de suponerlo, y solo puede citar convocatorias que existen en la base.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import app
from services import accounts, assistant, company_documents, db, rubros, tenders

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
PASSWORD = "Clave de prueba larga 2026"


# ---------------------------------------------------------------------------
# Base de convocatorias (SQLite en memoria)
# ---------------------------------------------------------------------------
@pytest.fixture
def tenders_db():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)

    with factory.begin() as session:
        for code, name, count in (("1510", "Combustibles y lubricantes", 10),
                                  ("2510", "Vehículos de motor", 5),
                                  ("3011", "Ej.: Cemento, hormigón premezclado", 3)):
            session.add(db.RubroCatalog(code=code, name=name, tenders_month=count))

        def tender(code, rubro, stage, days_ago, **extra):
            row = db.Tender(ocid=f"ocds-dgv273-seacev3-{code}", code=code,
                            title=extra.pop("title", f"ADQUISICION DE BIENES {code}"),
                            entity="GOBIERNO REGIONAL DE LA LIBERTAD", department="LA LIBERTAD",
                            method="Subasta Inversa Electrónica", rubros=rubro, state="vigente",
                            stage=stage, published_at=NOW - timedelta(days=days_ago), **extra)
            row.documents.append(db.TenderDocument(title="Bases Administrativas",
                                                   url=f"https://seace.gob.pe/doc/{code}.pdf", is_bases=True))
            session.add(row)
            return row

        t1 = tender("2001", "1510", "administrativas", 2, requirements_status="extraido",
                    enquiry_start=NOW - timedelta(days=10), enquiry_end=NOW + timedelta(days=3),
                    schedule=[{"key": "ofertas", "name": "Presentación de ofertas",
                               "start": "2026-11-20", "end": None, "page": 5,
                               "text": "Presentación de ofertas 20/11/2026"}])
        t1.chunks.append(db.RequirementChunk(
            chunk_index=0, page=4, source="texto", file_name="bases_2001.pdf",
            text="Requisito: inscripción vigente en el Registro de Hidrocarburos de OSINERGMIN. "
                 "Experiencia del postor: monto facturado acumulado de S/ 150,000.00 en venta de combustibles.",
            embedding=[0.1] * 768))

        t2 = tender("2002", "2510", "integradas", 1, requirements_status="extraido",
                    deadline_at=NOW + timedelta(days=8))
        t2.chunks.append(db.RequirementChunk(
            chunk_index=0, page=7, source="texto", file_name="bases_2002.pdf",
            text="Experiencia del postor en la especialidad: facturación acumulada de S/ 200,000.00 "
                 "en venta de vehículos similares.",
            embedding=[0.2] * 768))

        tender("2003", "3011", "buena_pro", 1, requirements_status="extraido")
        tender("2004", "1510", "administrativas", 3, requirements_status="pendiente")

    yield factory
    tenders.use_session_factory(None)
    engine.dispose()


# ---------------------------------------------------------------------------
# Cuentas, perfiles y documentos
# ---------------------------------------------------------------------------
def make_user(factory, email="empresa@example.test", rubros_text="Combustibles y lubricantes",
              amount="200000", ruc="20123456789", profile=True):
    uid = accounts.create_user(factory, email, PASSWORD, "Empresa de prueba")
    if profile:
        accounts.save_profile(factory, uid, {
            "company_name": "Empresa de prueba", "ruc": ruc,
            "business_activity": "Venta de bienes al Estado", "rubros": rubros_text,
            "monthly_sales": Decimal("10000"), "experience_years": 3,
            "similar_contracts_amount": Decimal(amount),
            "specialized_staff": "Un ingeniero con tres años de experiencia"})
    return uid


def add_document(factory, uid, kind, expires_on):
    with factory.begin() as session:
        company = session.scalar(select(accounts.Company).where(accounts.Company.user_id == uid))
        session.add(company_documents.CompanyDocument(
            company_id=company.id, filename=f"{kind}.pdf", document_type=kind,
            valid_from=date(2025, 1, 1), expires_on=expires_on, size_bytes=1200, page_count=1,
            sha256=f"{kind}-{uid}".ljust(64, "0")[:64], content=b"%PDF-1.4"))


def ask(factory, uid, query, **kwargs):
    """El asistente sin Gemini: el modelo simulado devuelve None y se usa el texto determinista."""
    return assistant.ask_assistant(query, uid, factory, email="empresa@example.test",
                                   today=TODAY, call_model=kwargs.pop("call_model", lambda prompt: None),
                                   **kwargs)


# ---------------------------------------------------------------------------
# Casos 1-10: preguntas de prueba con respuesta esperada
# ---------------------------------------------------------------------------
def test_caso_01_sin_rubro_pide_el_producto_con_una_frase(account_factory, tenders_db):
    """«¿A qué puedo postular?» sin perfil ni rubro: pregunta qué vende, no exige RUC."""
    uid = make_user(account_factory, profile=False)
    result = ask(account_factory, uid, "Hola, ¿a qué convocatorias puedo postular?")
    assert result["status"] == "need_rubro"
    assert "vende" in result["response"].lower()
    assert "RUC" not in result["response"]
    assert "2001" not in result["response"]


def test_caso_02_usa_el_perfil_guardado_sin_repetirlo_en_el_mensaje(account_factory, tenders_db):
    """Perfil con rubros de combustibles y RNP vigente: propone las convocatorias del rubro."""
    uid = make_user(account_factory, rubros_text="Combustibles y lubricantes", amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    result = ask(account_factory, uid, "¿A qué puedo postular?")
    assert "2001" in result["response"]
    assert "2003" not in result["response"]      # buena pro: no es postulable
    assert "Ley N.° 32069" in result["response"] and "OECE" in result["response"]


def test_caso_03_toma_el_rubro_del_mensaje_del_catalogo(account_factory, tenders_db):
    """Perfil sin rubro útil; el mensaje menciona «combustible diésel» y se busca en el catálogo."""
    uid = make_user(account_factory, rubros_text="Servicios generales", amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    result = ask(account_factory, uid, "Vendo combustible diésel, ¿a qué puedo postular?")
    assert "2001" in result["response"]
    assert any(item["code"] == "1510" for item in result["rubros"])


def test_caso_04_sin_rnp_cargado_avisa_pero_no_descarta(account_factory, tenders_db):
    """Sin constancia de RNP cargada: lo advierte y deja la convocatoria «por verificar»."""
    uid = make_user(account_factory, amount="200000")
    result = ask(account_factory, uid, "Vendo combustibles, ¿puedo postular?")
    assert "RNP" in result["response"]
    assert "⚪" in result["response"] or "Por verificar" in result["response"]


def test_caso_04b_si_el_usuario_dice_que_no_tiene_rnp_es_rojo(account_factory, tenders_db):
    """Si afirma que no tiene RNP, ahí sí es una descalificación segura."""
    uid = make_user(account_factory, amount="200000")
    result = ask(account_factory, uid, "Vendo combustibles, no tengo RNP")
    assert "🔴" in result["response"]


def test_caso_05_rnp_vencido_lo_avisa(account_factory, tenders_db):
    """Constancia de RNP vencida: lo dice con la palabra «vencida»."""
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2026, 1, 1))
    result = ask(account_factory, uid, "Vendo combustibles, ¿puedo postular?")
    assert "vencida" in result["response"].lower()


def test_caso_06_experiencia_insuficiente_es_rojo_y_cita_el_monto(account_factory, tenders_db):
    """Facturación de S/ 50,000 frente a S/ 150,000 exigidos: rojo y monto citado."""
    uid = make_user(account_factory, amount="50000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    result = ask(account_factory, uid, "Vendo combustibles")
    assert "🔴" in result["response"]
    assert "150,000.00" in result["response"]
    assert "pág. 4" in result["response"]


def test_caso_07_experiencia_suficiente_es_verde(account_factory, tenders_db):
    """Facturación de S/ 300,000 frente a S/ 200,000 de vehículos: verde en 2002."""
    uid = make_user(account_factory, rubros_text="Vehículos de motor", amount="300000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    result = ask(account_factory, uid, "Vendo camionetas")
    assert "2002" in result["response"]
    assert "🟢" in result["response"]


def test_caso_08_avisa_cuando_el_plazo_esta_por_vencer(account_factory, tenders_db):
    """El plazo de consultas vence en 3 días: lo avisa expresamente."""
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    result = ask(account_factory, uid, "Vendo combustibles")
    assert "vence en 3 días" in result["response"]
    assert "por vencer" in result["response"].lower()
    assert "Presentación de ofertas: 20 nov 2026" in result["response"]


def test_caso_09_ignora_codigos_inventados_por_el_modelo(account_factory, tenders_db):
    """Si Gemini cita un código que no existe, se descarta y se responde con el texto determinista."""
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))

    def inventa(prompt):
        return {"intro": "Estas son sus opciones.",
                "items": [{"codigo": "9999", "texto": "Convocatoria inventada."},
                          {"codigo": "2001", "texto": "Puede postular a esta convocatoria."}],
                "cierre": "Revise las bases."}

    result = ask(account_factory, uid, "Vendo combustibles", call_model=inventa)
    assert "9999" not in result["response"]
    assert "2001" in result["response"]


def test_caso_10_sin_base_de_convocatorias_no_inventa_nada(account_factory, tenders_db, monkeypatch):
    """Si la base de convocatorias no responde, lo dice y no propone ninguna."""
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))

    def caida(*args, **kwargs):
        raise tenders.TendersUnavailable("sin_conexion")

    monkeypatch.setattr(tenders, "open_tenders", caida)
    result = ask(account_factory, uid, "Vendo combustibles")
    assert result["status"] == "db_unavailable"
    assert "No puedo consultar" in result["response"]
    assert "2001" not in result["response"]


# ---------------------------------------------------------------------------
# Reglas transversales
# ---------------------------------------------------------------------------
def test_el_modelo_no_puede_colar_un_codigo_en_la_introduccion(account_factory, tenders_db):
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))

    def inventa(prompt):
        return {"intro": "Revise el proceso 8888888.", "items": [{"codigo": "2001", "texto": "Viable."}],
                "cierre": "Fin."}

    result = ask(account_factory, uid, "Vendo combustibles", call_model=inventa)
    assert "8888888" not in result["response"]
    assert "2001" in result["response"]


def test_la_respuesta_siempre_cita_la_ley_32069_y_el_oece(account_factory, tenders_db):
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    for query in ("Vendo combustibles", "¿Qué documentos me faltan?", "Hola"):
        result = ask(account_factory, uid, query)
        assert "32069" in result["response"] and "OECE" in result["response"]


def test_el_rubro_del_perfil_gana_sobre_una_consulta_generica(account_factory, tenders_db):
    uid = make_user(account_factory, rubros_text="Cemento", amount="0")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    result = ask(account_factory, uid, "¿A qué puedo postular?")
    assert any(item["code"] == "3011" for item in result["rubros"])
    assert "2003" not in result["response"]       # la única de cemento está en buena pro


def test_sin_bases_leidas_queda_por_verificar(account_factory, tenders_db):
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    result = ask(account_factory, uid, "Vendo combustibles")
    assert "2004" in result["response"]
    assert "⚪" in result["response"] or "Por verificar" in result["response"]


def test_entiende_el_producto_y_el_monto_del_propio_mensaje(account_factory, tenders_db):
    """«tengo 3000 mil ventas en autos» aporta rubro 2510 y S/ 3,000,000 de experiencia."""
    uid = make_user(account_factory, profile=False)
    result = ask(account_factory, uid, "tengo 3000 mil ventas en autos")
    assert any(item["code"] == "2510" for item in result["rubros"])
    assert "2002" in result["response"]
    assert "3,000,000" in result["response"]


def test_el_rubro_se_recuerda_en_el_siguiente_turno(account_factory, tenders_db):
    uid = make_user(account_factory, rubros_text="Servicios generales", amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    first = ask(account_factory, uid, "Vendo combustible diésel")
    assert first["rubros"]
    second = ask(account_factory, uid, "¿Y cuáles vencen pronto?", remembered_rubros=first["rubros"])
    assert "2001" in second["response"]


# ---------------------------------------------------------------------------
# Widget flotante
# ---------------------------------------------------------------------------
def test_los_documentos_faltantes_o_vencidos_se_informan(account_factory, tenders_db):
    """«¿Qué documentos me faltan?» responde con el estado real de cada tipo."""
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2026, 1, 1))       # vencido
    add_document(account_factory, uid, "experiencia", date(2027, 1, 1))
    result = ask(account_factory, uid, "¿Qué documentos me faltan o están vencidos?")
    assert result["status"] == "documents"
    assert "vencido" in result["response"].lower()
    assert "**Personal:** no cargado" in result["response"]
    assert "Documentos de mi empresa" in result["response"]


def test_sin_rubro_resoluble_pide_el_producto(account_factory, tenders_db):
    """Perfil con un texto que no corresponde a ningún rubro: pide el producto, no lo inventa."""
    uid = make_user(account_factory, rubros_text="Servicios generales", amount="200000")
    result = ask(account_factory, uid, "¿A qué puedo postular?")
    assert result["status"] == "need_rubro"
    assert "catálogo" in result["response"].lower()
    assert "2001" not in result["response"]


def test_sin_base_de_convocatorias_configurada_responde_con_aviso(account_factory, monkeypatch):
    """Sin DATABASE_URL el grafo no debe romperse: avisa y sigue siendo útil."""
    import config
    monkeypatch.setattr(config, "DATABASE_URL", None)
    tenders.use_session_factory(None)
    uid = make_user(account_factory, amount="200000")
    result = ask(account_factory, uid, "Vendo combustibles, ¿a qué puedo postular?")
    assert result["status"] == "db_unavailable"
    assert "DATABASE_URL" in result["response"]
    assert "No se pudo completar" not in result["response"]
    assert "32069" in result["response"]


def test_sin_base_los_rubros_piloto_siguen_disponibles(monkeypatch):
    import config
    monkeypatch.setattr(config, "DATABASE_URL", None)
    tenders.use_session_factory(None)
    names = tenders.rubro_names()
    assert "1510" in names and "Combustibles" in names["1510"]


def test_gemini_redacta_los_avisos_cuando_esta_configurado(account_factory):
    """Con Gemini disponible, los avisos (perfil, saludo) se reescriben con su texto."""
    uid = make_user(account_factory, profile=False)
    result = ask(account_factory, uid, "hola",
                 call_model=lambda prompt: {"respuesta": "Hola, soy su asesor de licitaciones."})
    assert "asesor de licitaciones" in result["response"]
    assert "RUC" not in result["response"]


def test_gemini_no_puede_inventar_codigos_al_redactar_un_aviso(account_factory):
    """Si la reescritura introduce un código de convocatoria inexistente, se descarta."""
    uid = make_user(account_factory, profile=False)
    result = ask(account_factory, uid, "hola",
                 call_model=lambda prompt: {"respuesta": "Postule al proceso 9999999."})
    assert "9999999" not in result["response"]
    assert "Mi empresa" in result["response"]


def test_el_resumen_separa_el_detalle_largo(account_factory, tenders_db):
    """El chat recibe un resumen y el detalle completo va aparte, para no llenar la conversación."""
    uid = make_user(account_factory, amount="200000")
    add_document(account_factory, uid, "rnp", date(2027, 1, 1))
    result = ask(account_factory, uid, "Vendo combustibles")
    assert assistant.DETAIL_MARKER in result["response"]
    summary, _, detail = result["response"].partition(assistant.DETAIL_MARKER)
    assert "2001" not in summary and "2001" in detail
    assert "32069" in summary
    assert result["detail_count"] >= 1


def test_el_widget_devuelve_resumen_y_detalle(signed_in_client, monkeypatch):
    """El endpoint entrega `html` corto y `detail` aparte con el conteo de convocatorias."""
    from services import assistant

    monkeypatch.setattr(assistant, "ask_assistant", lambda *args, **kwargs: {
        "response": "Resumen corto" + assistant.DETAIL_MARKER + "Detalle largo",
        "status": "answered", "rubros": [], "missing": [], "profile": {}, "detail_count": 3})

    response = signed_in_client.post("/asistente/mensaje", data={"query": "x"})
    body = response.get_json()
    assert "Resumen corto" in body["html"]
    assert "Detalle largo" not in body["html"]
    assert "Detalle largo" in body["detail"]
    assert body["detail_count"] == 3


def test_el_widget_aparece_en_todas_las_pantallas_con_sesion(signed_in_client):
    html = signed_in_client.get("/").get_data(as_text=True)
    assert "data-chat" in html and "/asistente/mensaje" in html
    assert "Ley N.° 32069" in html


def test_el_widget_tiene_los_enganches_que_usa_el_javascript(signed_in_client):
    """Regresión: sin `data-chat-panel` el clic lanzaba TypeError y el panel no abría."""
    html = signed_in_client.get("/").get_data(as_text=True)
    for hook in ("data-chat", "data-chat-launcher", "data-chat-panel", "data-chat-log",
                 "data-chat-form", "data-chat-input", "data-chat-send", "data-chat-close",
                 "data-chat-clear", "data-chat-chips"):
        assert hook in html, f"Falta el enganche {hook} en el widget"


def test_el_widget_no_aparece_sin_sesion(account_client):
    html = account_client.get("/").get_data(as_text=True)
    assert "data-chat" not in html


def test_el_widget_responde_json_sin_recargar_la_pantalla(signed_in_client, account_factory, tenders_db):
    response = signed_in_client.post("/asistente/mensaje",
                                     data={"query": "¿A qué puedo postular?"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True and "<p>" in body["html"]


def test_el_endpoint_del_widget_exige_sesion(account_client):
    response = account_client.post("/asistente/mensaje", data={"query": "Hola"})
    assert response.status_code == 302 and "/ingresar" in response.location


def test_el_endpoint_del_widget_exige_csrf(signed_in_client):
    response = signed_in_client.open("/asistente/mensaje", method="POST", data={"query": "Hola"})
    assert response.status_code == 400


def test_la_pagina_antigua_del_asistente_ya_no_existe(signed_in_client):
    response = signed_in_client.get("/asistente")
    assert response.status_code == 302 and response.location.endswith("/")


def test_reiniciar_la_conversacion_borra_el_historial(signed_in_client, account_factory, tenders_db):
    signed_in_client.post("/asistente/mensaje", data={"query": "Vendo cemento"})
    assert signed_in_client.get("/").get_data(as_text=True).count("lf-chat-msg") > 0
    assert signed_in_client.post("/asistente/limpiar").get_json()["ok"] is True


def test_el_rubro_del_catalogo_se_resuelve_por_su_nombre(tenders_db):
    with tenders_db() as session:
        names = rubros.names(session)
    assert rubros.match("vendo cemento y hormigón", names)[0]["code"] == "3011"
    assert rubros.match("venta de combustibles y lubricantes", names)[0]["code"] == "1510"
    assert rubros.match("vendo autos y camionetas", names)[0]["code"] == "2510"
    assert rubros.match("consultoría de software", names) == []


def test_los_enlaces_del_chat_no_permiten_inyectar_atributos():
    """El filtro Markdown acepta enlaces internos y http(s), no comillas ni javascript:."""
    from services.formatting import markdown

    assert '<a href="/convocatorias/2001">Ver ficha</a>' in markdown("[Ver ficha](/convocatorias/2001)")
    # Una URL con comillas no se convierte en enlace: se queda como texto escapado.
    hostil = markdown('[x](http://a/" onmouseover="alert(1))')
    assert "<a" not in hostil and 'href="http://a/"' not in hostil
    assert 'href="javascript:' not in markdown("[x](javascript:alert(1))")
