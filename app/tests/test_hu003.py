
import statistics

import pandas as pd
import pytest

import datos
import llm
from agentes import analista, clasificador
from app import app, formato_numero, formato_porcentaje

CEMENTO_I = 12771     # 1,388 adjudicaciones, 4 sin dato de postores
MALLA = 1414          # 9 adjudicaciones
BUZO = 392753         # 15 adjudicaciones, ninguna con dato de postores
INEXISTENTE = 999999


@pytest.fixture
def sin_modelo(monkeypatch):
    """Si algo intenta llamar al modelo de lenguaje, la prueba falla."""
    def prohibido(*args, **kwargs):
        raise AssertionError("HU-003 no debe usar el modelo de lenguaje")
    monkeypatch.setattr(llm, "_llamar_gemini", prohibido)
    monkeypatch.setattr(llm, "generar_json", prohibido)
    monkeypatch.setattr(clasificador, "clasificar", prohibido)


def filas_de(codigo):
    tabla = datos.ADJUDICACIONES
    return tabla[(tabla["codigoitem"] == codigo) & (tabla["unidad_medida"] == datos.BIENES[codigo]["unidad"])]


# --- Agente analista: indicadores de competencia --------------------------------

def test_mediana_de_postores_por_proceso_sin_contar_los_que_no_tienen_dato(sin_modelo):
    r = analista.analizar(CEMENTO_I)
    postores = filas_de(CEMENTO_I)["n_postores"].dropna().astype(int).tolist()
    assert r["n_con_postores"] == len(postores) < r["n_adjudicaciones"]
    assert r["mediana_postores"] == statistics.median(postores)


def test_porcentaje_de_procesos_con_un_solo_postor(sin_modelo):
    r = analista.analizar(CEMENTO_I)
    postores = filas_de(CEMENTO_I)["n_postores"].dropna().astype(int)
    assert r["n_postor_unico"] == int((postores == 1).sum())
    assert r["pct_postor_unico"] == pytest.approx(100 * (postores == 1).mean())


def test_adjudicaciones_de_entidades_de_la_libertad(sin_modelo):
    r = analista.analizar(CEMENTO_I)
    assert r["n_la_libertad"] == int((filas_de(CEMENTO_I)["entidad_departamento"] == "LA LIBERTAD").sum())


def test_con_postores_simulados_los_indicadores_son_los_esperados(monkeypatch, sin_modelo):
    """Caso armado a mano: 5 procesos con 1, 1, 3, 4 postores y uno sin dato."""
    filas = pd.DataFrame({
        "codigoitem": CEMENTO_I, "unidad_medida": "Unidad", "anio": 2024, "codigoconvocatoria": range(5),
        "precio_unitario": [10.0, 11.0, 12.0, 13.0, 14.0],
        "n_postores": pd.array([1, 1, 3, 4, None], dtype="Int64"),
        "entidad_departamento": ["LA LIBERTAD", "LIMA", "LA LIBERTAD", "CUSCO", "LA LIBERTAD"],
    })
    monkeypatch.setattr(datos, "ADJUDICACIONES", filas)
    r = analista.analizar(CEMENTO_I)
    assert r["n_con_postores"] == 4
    assert r["mediana_postores"] == 2.0
    assert r["n_postor_unico"] == 2 and r["pct_postor_unico"] == 50.0
    assert r["n_la_libertad"] == 3


def test_bien_sin_datos_de_postores(sin_modelo):
    r = analista.analizar(BUZO)
    assert r["n_adjudicaciones"] > 0 and r["n_con_postores"] == 0
    assert r["mediana_postores"] is None and r["pct_postor_unico"] is None


def test_bien_inexistente_no_tiene_competencia(sin_modelo):
    r = analista.analizar(INEXISTENTE)
    assert r["n_con_postores"] == 0 and r["n_la_libertad"] == 0
    assert r["mediana_postores"] is None and r["pct_postor_unico"] is None


# --- Pantalla de mercado: sección de competencia ---------------------------------

@pytest.fixture
def cliente():
    app.config["TESTING"] = True
    return app.test_client()


def html_mercado(cliente, codigo):
    with cliente.session_transaction() as sesion:
        sesion["codigo"] = codigo
        sesion["candidatos"] = [codigo]
    return cliente.get("/mercado").get_data(as_text=True)


def test_pantalla_muestra_los_tres_indicadores(cliente, sin_modelo):
    r = analista.analizar(CEMENTO_I)
    html = html_mercado(cliente, CEMENTO_I)
    for texto in ("Competencia en el mercado", "Postores (mediana)", "por proceso", "Postor único",
                  "La Libertad", formato_numero(r["mediana_postores"]), formato_porcentaje(r["pct_postor_unico"]),
                  f"{r['n_la_libertad']} fueron de entidades de La Libertad",
                  f"{r['n_adjudicaciones'] - r['n_con_postores']} adjudicaciones no tienen registro de postores"):
        assert texto in html, texto


def test_la_competencia_tambien_aparece_con_evidencia_insuficiente(cliente, sin_modelo):
    html = html_mercado(cliente, MALLA)
    assert "Sin evidencia suficiente para un veredicto" in html
    assert "Competencia en el mercado" in html and "por proceso" in html


def test_pantalla_avisa_cuando_no_hay_datos_de_postores(cliente, sin_modelo):
    html = html_mercado(cliente, BUZO)
    assert "no tienen registro de postores, por eso no se muestra la competencia" in html
    assert "sin datos" in html


def test_formatos_de_numero_y_porcentaje():
    assert formato_numero(5.0) == "5"
    assert formato_numero(2.5) == "2.5"
    assert formato_porcentaje(0.36127) == "0.4%"
    assert formato_porcentaje(50.0) == "50.0%"