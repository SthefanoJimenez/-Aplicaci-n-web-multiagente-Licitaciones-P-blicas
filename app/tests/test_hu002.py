"""Pruebas de HU-002: consultar el rango de precios (no consumen la API de Gemini).

Ejecutar desde la carpeta app/:  pytest
"""
import numpy as np
import pandas as pd
import pytest

import datos
import llm
import orquestador
import reglas
from agentes import analista, clasificador
from app import app, formato_soles, unidad_en_texto

CEMENTO_I = 12771          
DIESEL = 233280            
MALLA = 1414               
INEXISTENTE = 999999


@pytest.fixture
def sin_modelo(monkeypatch):
    """Si algo intenta llamar al modelo de lenguaje, la prueba falla."""
    def prohibido(*args, **kwargs):
        raise AssertionError("HU-002 no debe usar el modelo de lenguaje")
    monkeypatch.setattr(llm, "_llamar_gemini", prohibido)
    monkeypatch.setattr(llm, "generar_json", prohibido)
    monkeypatch.setattr(clasificador, "clasificar", prohibido)


def test_calcula_p25_mediana_y_p75_con_interpolacion_lineal(sin_modelo):
    precios = datos.ADJUDICACIONES.loc[datos.ADJUDICACIONES["codigoitem"] == CEMENTO_I, "precio_unitario"]
    esperado = np.percentile(precios.to_numpy(), [25, 50, 75], method="linear")
    r = analista.analizar(CEMENTO_I)
    assert [r["p25"], r["mediana"], r["p75"]] == pytest.approx(esperado.tolist(), abs=1e-9)
    assert r["p25"] <= r["mediana"] <= r["p75"]


def test_informa_unidad_numero_de_adjudicaciones_y_periodo(sin_modelo):
    r = analista.analizar(DIESEL)
    filas = datos.ADJUDICACIONES[datos.ADJUDICACIONES["codigoitem"] == DIESEL]
    assert r["unidad"] == datos.BIENES[DIESEL]["unidad"] == "Galon"
    assert r["n_adjudicaciones"] == len(filas)
    assert (r["anio_desde"], r["anio_hasta"]) == (filas["anio"].min(), filas["anio"].max())


def test_filtra_por_codigo_y_unidad(monkeypatch, sin_modelo):
    """Una compra del mismo código en otra unidad no entra en el cálculo."""
    antes = analista.analizar(CEMENTO_I)
    intrusa = pd.DataFrame([{"codigoitem": CEMENTO_I, "unidad_medida": "Kilogramo", "anio": 2025,
                             "codigoconvocatoria": 1, "precio_unitario": 999999.0, "n_postores": 1,
                             "entidad_departamento": "LIMA"}])
    monkeypatch.setattr(datos, "ADJUDICACIONES", pd.concat([datos.ADJUDICACIONES, intrusa], ignore_index=True))
    despues = analista.analizar(CEMENTO_I)
    assert despues["n_adjudicaciones"] == antes["n_adjudicaciones"]
    assert despues["p75"] == antes["p75"]


def test_bien_con_menos_del_minimo_queda_sin_evidencia_suficiente(sin_modelo):
    r = analista.analizar(MALLA)
    assert reglas.MIN_CATALOGO <= r["n_adjudicaciones"] < reglas.MIN_VEREDICTO
    assert r["evidencia"] == "Sin evidencia suficiente"
    assert r["mediana"] is not None   # el rango se calcula igual, como referencia


def test_bien_sin_adjudicaciones_no_tiene_rango(sin_modelo):
    r = analista.analizar(INEXISTENTE)
    assert r["n_adjudicaciones"] == 0
    assert r["p25"] is r["mediana"] is r["p75"] is None
    assert r["evidencia"] == "Sin evidencia para evaluar"


def test_resultado_identico_en_llamadas_repetidas(sin_modelo):
    assert analista.analizar(CEMENTO_I) == analista.analizar(CEMENTO_I)


# --- Orquestador: tramo 2 ------------------------------------------------------

def test_el_tramo_de_mercado_tiene_al_agente_analista_y_tres_salidas():
    nodos = set(orquestador.TRAMO_MERCADO.get_graph().nodes)
    assert {"agente_analista", "mostrar_rango", "rango_referencial", "sin_evidencia_para_evaluar"} <= nodos


@pytest.mark.parametrize("codigo, resultado", [
    (CEMENTO_I, orquestador.RANGO_CON_EVIDENCIA),
    (MALLA, orquestador.RANGO_REFERENCIAL),
    (INEXISTENTE, orquestador.SIN_EVIDENCIA_PARA_EVALUAR),
])
def test_el_grafo_decide_la_salida_segun_la_evidencia(codigo, resultado, sin_modelo):
    assert orquestador.analizar_mercado(codigo)["resultado"] == resultado


# --- Pantalla de mercado -------------------------------------------------------

@pytest.fixture
def cliente():
    app.config["TESTING"] = True
    return app.test_client()


def con_codigo(cliente, codigo, candidatos=None):
    with cliente.session_transaction() as sesion:
        sesion["codigo"] = codigo
        sesion["candidatos"] = candidatos or [codigo]
        sesion["descripcion"] = "cemento en bolsa"


def test_sin_codigo_confirmado_vuelve_al_inicio(cliente):
    respuesta = cliente.get("/mercado")
    assert respuesta.status_code == 302 and respuesta.headers["Location"].endswith("/")


def test_pantalla_muestra_rango_unidad_adjudicaciones_y_periodo(cliente, sin_modelo):
    con_codigo(cliente, CEMENTO_I)
    html = cliente.get("/mercado").get_data(as_text=True)
    r = analista.analizar(CEMENTO_I)
    for texto in ("P25", "Mediana", "P75", "por unidad", formato_soles(r["p25"]), formato_soles(r["mediana"]),
                  formato_soles(r["p75"]), f"{r['n_adjudicaciones']:,} adjudicaciones", "2023–2025",
                  "Competitivo", "Con cautela", "Alto costo", "Ingresar mi costo"):
        assert texto in html, texto
    assert "Sin evidencia suficiente" not in html


def test_pantalla_avisa_cuando_no_alcanza_el_minimo(cliente, sin_modelo):
    con_codigo(cliente, MALLA)
    html = cliente.get("/mercado").get_data(as_text=True)
    assert "Sin evidencia suficiente para un veredicto" in html
    assert f"se necesitan al menos {reglas.MIN_VEREDICTO}" in html
    assert "Ingresar mi costo" not in html


def test_cambiar_codigo_vuelve_a_los_candidatos_sin_llamar_al_modelo(cliente, sin_modelo):
    con_codigo(cliente, CEMENTO_I, candidatos=[12953, CEMENTO_I])
    html = cliente.get("/candidatos").get_data(as_text=True)
    assert "CUBSO 12953" in html and f'value="{CEMENTO_I}" checked' in html


def test_formatos_de_soles_y_unidad():
    assert formato_soles(1234.5) == "S/ 1,234.50"
    assert formato_soles(0.0456) == "S/ 0.0456"
    assert unidad_en_texto("Galon") == "galón"
    assert unidad_en_texto("M3") == "m³"
    assert unidad_en_texto("Kilogramo") == "kilogramo"