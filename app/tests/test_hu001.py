"""Pruebas de HU-001 con un modelo simulado (no consumen la API de Gemini).

Ejecutar desde la carpeta app/:  pytest
"""
import pytest

import datos
import llm
import orquestador
from agentes import clasificador
from app import app

CEMENTO_I, CEMENTO_IP, BARRA_MEDIA, DIESEL = 12771, 12953, 130019, 233280
INEXISTENTE = 999999


def modelo_simulado(reformulacion="", codigos=(), falla_reformular=False, falla_elegir=False):
    """Imita al modelo: responde distinto según sea el paso de reformular o de elegir."""
    def llamar(prompt):
        if "Descripción del empresario:" in prompt:
            if falla_reformular:
                raise llm.ErrorModelo("simulado")
            return {"descripcion": reformulacion}
        if falla_elegir:
            raise llm.ErrorModelo("simulado")
        return {"codigos": list(codigos)}
    return llamar


#

def test_valida_y_descarta_codigos_inexistentes_y_repetidos():
    validos, descartados = clasificador.validar([CEMENTO_I, INEXISTENTE, CEMENTO_I, "texto", CEMENTO_IP])
    assert validos == [CEMENTO_I, CEMENTO_IP]
    assert descartados == [INEXISTENTE, "texto"]


def test_devuelve_hasta_tres_candidatos_en_orden_con_codigo_nombre_y_unidad():
    llamar = modelo_simulado("cemento", [DIESEL, INEXISTENTE, CEMENTO_I, CEMENTO_IP, BARRA_MEDIA])
    resultado = clasificador.clasificar("cemento en bolsa", llamar_modelo=llamar)
    assert [c["codigo"] for c in resultado["candidatos"]] == [DIESEL, CEMENTO_I, CEMENTO_IP]
    assert set(resultado["candidatos"][0]) == {"codigo", "nombre", "unidad"}
    assert resultado["descartados"] == [INEXISTENTE]


def test_todos_los_candidatos_existen_en_el_catalogo():
    llamar = modelo_simulado("x", [INEXISTENTE, 1, 2, CEMENTO_I])
    resultado = clasificador.clasificar("algo", llamar_modelo=llamar)
    assert all(c["codigo"] in datos.CODIGOS_VALIDOS for c in resultado["candidatos"])


def test_si_falla_la_reformulacion_continua_con_la_descripcion_original():
    llamar = modelo_simulado(codigos=[CEMENTO_I], falla_reformular=True)
    resultado = clasificador.clasificar("cemento portland tipo I", llamar_modelo=llamar)
    assert resultado["descripcion_reformulada"] is None
    assert [c["codigo"] for c in resultado["candidatos"]] == [CEMENTO_I]


def test_si_falla_la_eleccion_se_informa_el_error():
    with pytest.raises(llm.ErrorModelo):
        clasificador.clasificar("cemento", llamar_modelo=modelo_simulado("cemento", falla_elegir=True))


def test_la_preseleccion_recupera_descripciones_coloquiales_gracias_a_la_reformulacion():
    # Caso de SP-004: "fierro" no comparte palabras con "barra para construcción"
    assert BARRA_MEDIA not in clasificador.preseleccionar("fierro corrugado de media pulgada")
    assert BARRA_MEDIA in clasificador.preseleccionar(
        "fierro corrugado de media pulgada", "barra de acero corrugado para construccion de 1/2 in")


# --- Orquestador --------------------------------------------------------------

@pytest.mark.parametrize("descripcion", ["", "   ", None])
def test_rechaza_descripciones_vacias(descripcion):
    with pytest.raises(orquestador.DescripcionInvalida):
        orquestador.identificar_bien(descripcion)


def test_rechaza_descripciones_demasiado_largas():
    with pytest.raises(orquestador.DescripcionInvalida):
        orquestador.identificar_bien("a" * 301)


def test_sin_candidatos_validos_da_sin_evidencia_para_evaluar(monkeypatch):
    monkeypatch.setattr(clasificador, "clasificar",
                        lambda d: {"candidatos": [], "descripcion_reformulada": None, "descartados": [INEXISTENTE]})
    assert orquestador.identificar_bien("servicio de limpieza")["resultado"] == orquestador.SIN_EVIDENCIA_PARA_EVALUAR


# --- Pantallas -----------------------------------------------------------------

@pytest.fixture
def cliente():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def simular_candidatos(monkeypatch, codigos):
    monkeypatch.setattr(clasificador, "clasificar", lambda d: {
        "candidatos": [datos.BIENES[c] for c in codigos], "descripcion_reformulada": None, "descartados": []})


def test_pantalla_de_descripcion(cliente):
    html = cliente.get("/").get_data(as_text=True)
    assert "¿Qué bien vende su empresa?" in html


def test_descripcion_vacia_muestra_mensaje(cliente):
    respuesta = cliente.post("/candidatos", data={"descripcion": "   "})
    assert respuesta.status_code == 400
    assert "Escriba una descripción del bien" in respuesta.get_data(as_text=True)


def test_muestra_candidatos_con_el_primero_preseleccionado(cliente, monkeypatch):
    simular_candidatos(monkeypatch, [CEMENTO_I, CEMENTO_IP])
    html = cliente.post("/candidatos", data={"descripcion": "cemento"}).get_data(as_text=True)
    assert html.count('type="radio"') == 2
    assert f'value="{CEMENTO_I}" checked' in html
    assert f'value="{CEMENTO_IP}" checked' not in html
    assert datos.BIENES[CEMENTO_I]["nombre"] in html


def test_confirmar_un_candidato_lleva_al_mercado(cliente, monkeypatch):
    simular_candidatos(monkeypatch, [CEMENTO_I, CEMENTO_IP])
    cliente.post("/candidatos", data={"descripcion": "cemento"})
    respuesta = cliente.post("/confirmar", data={"codigo": CEMENTO_IP})
    assert respuesta.headers["Location"].endswith("/mercado")
    assert datos.BIENES[CEMENTO_IP]["nombre"] in cliente.get("/mercado").get_data(as_text=True)


def test_no_acepta_un_codigo_que_no_fue_ofrecido(cliente, monkeypatch):
    simular_candidatos(monkeypatch, [CEMENTO_I])
    cliente.post("/candidatos", data={"descripcion": "cemento"})
    respuesta = cliente.post("/confirmar", data={"codigo": DIESEL})
    assert respuesta.headers["Location"].endswith("/")


def test_ninguno_corresponde_vuelve_a_la_descripcion_con_el_texto_anterior(cliente, monkeypatch):
    simular_candidatos(monkeypatch, [CEMENTO_I])
    cliente.post("/candidatos", data={"descripcion": "cemento en bolsa"})
    html = cliente.get("/?reintentar=1").get_data(as_text=True)
    assert "Pruebe describirlo de otra forma" in html
    assert "cemento en bolsa" in html


def test_sin_candidatos_muestra_sin_evidencia_para_evaluar(cliente, monkeypatch):
    simular_candidatos(monkeypatch, [])
    html = cliente.post("/candidatos", data={"descripcion": "servicio de limpieza"}).get_data(as_text=True)
    assert "Sin evidencia para evaluar" in html


def test_si_el_modelo_no_responde_se_conserva_la_descripcion(cliente, monkeypatch):
    def fallar(d):
        raise llm.ErrorModelo("simulado")
    monkeypatch.setattr(clasificador, "clasificar", fallar)
    respuesta = cliente.post("/candidatos", data={"descripcion": "cemento en bolsa"})
    html = respuesta.get_data(as_text=True)
    assert respuesta.status_code == 503
    assert 'value="cemento en bolsa"' in html and "Reintentar búsqueda" in html


# --- Orquestador con LangGraph ---------------------------------------------------

def test_el_tramo_1_tiene_los_nodos_del_pipeline():
    nodos = set(orquestador.TRAMO_IDENTIFICACION.get_graph().nodes)
    assert {"agente_clasificador", "mostrar_candidatos", "sin_evidencia_para_evaluar"} <= nodos


def test_el_grafo_decide_la_salida_segun_los_candidatos(monkeypatch):
    monkeypatch.setattr(clasificador, "clasificar", lambda d: {
        "candidatos": [datos.BIENES[CEMENTO_I]], "descripcion_reformulada": "cemento", "descartados": []})
    estado = orquestador.TRAMO_IDENTIFICACION.invoke({"descripcion": "cemento"})
    assert estado["resultado"] == orquestador.CANDIDATOS

    monkeypatch.setattr(clasificador, "clasificar", lambda d: {
        "candidatos": [], "descripcion_reformulada": None, "descartados": []})
    estado = orquestador.TRAMO_IDENTIFICACION.invoke({"descripcion": "servicio de limpieza"})
    assert estado["resultado"] == orquestador.SIN_EVIDENCIA_PARA_EVALUAR