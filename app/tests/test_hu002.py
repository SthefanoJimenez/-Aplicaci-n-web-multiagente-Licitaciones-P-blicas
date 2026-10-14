import numpy as np
import pandas as pd
import pytest

import orchestrator
import rules
from agents import classifier, market_analyst
from app import app, format_pen, unit_text
from services import data, llm

CEMENT_I = 12771           
DIESEL = 233280            
WIRE_MESH = 1414           
NONEXISTENT = 999999


@pytest.fixture
def no_model(monkeypatch):
    """Si algo intenta llamar al modelo de lenguaje, la prueba falla."""
    def forbidden(*args, **kwargs):
        raise AssertionError("HU-002 no debe usar el modelo de lenguaje")
    monkeypatch.setattr(llm, "_call_gemini", forbidden)
    monkeypatch.setattr(llm, "generate_json", forbidden)
    monkeypatch.setattr(classifier, "classify", forbidden)


def test_computes_p25_median_and_p75_with_linear_interpolation(no_model):
    prices = data.AWARDS.loc[data.AWARDS["codigoitem"] == CEMENT_I, "precio_unitario"]
    expected = np.percentile(prices.to_numpy(), [25, 50, 75], method="linear")
    r = market_analyst.analyze(CEMENT_I)
    assert [r["p25"], r["median"], r["p75"]] == pytest.approx(expected.tolist(), abs=1e-9)
    assert r["p25"] <= r["median"] <= r["p75"]


def test_reports_unit_number_of_awards_and_period(no_model):
    r = market_analyst.analyze(DIESEL)
    rows = data.AWARDS[data.AWARDS["codigoitem"] == DIESEL]
    assert r["unit"] == data.GOODS[DIESEL]["unit"] == "Galon"
    assert r["n_awards"] == len(rows)
    assert (r["year_from"], r["year_to"]) == (rows["anio"].min(), rows["anio"].max())


def test_filters_by_code_and_unit(monkeypatch, no_model):
    before = market_analyst.analyze(CEMENT_I)
    intruder = pd.DataFrame([{"codigoitem": CEMENT_I, "unidad_medida": "Kilogramo", "anio": 2025,
                              "codigoconvocatoria": 1, "precio_unitario": 999999.0, "n_postores": 1,
                              "entidad_departamento": "LIMA"}])
    monkeypatch.setattr(data, "AWARDS", pd.concat([data.AWARDS, intruder], ignore_index=True))
    after = market_analyst.analyze(CEMENT_I)
    assert after["n_awards"] == before["n_awards"]
    assert after["p75"] == before["p75"]


def test_good_below_minimum_has_insufficient_evidence(no_model):
    r = market_analyst.analyze(WIRE_MESH)
    assert rules.MIN_CATALOG <= r["n_awards"] < rules.MIN_VERDICT
    assert r["evidence"] == rules.INSUFFICIENT_EVIDENCE
    assert r["median"] is not None   


def test_good_without_awards_has_no_range(no_model):
    r = market_analyst.analyze(NONEXISTENT)
    assert r["n_awards"] == 0
    assert r["p25"] is r["median"] is r["p75"] is None
    assert r["evidence"] == rules.NO_EVIDENCE


def test_same_result_on_repeated_calls(no_model):
    assert market_analyst.analyze(CEMENT_I) == market_analyst.analyze(CEMENT_I)



def test_market_stage_has_analyst_agent_and_three_outputs():
    nodes = set(orchestrator.MARKET_STAGE.get_graph().nodes)
    assert {"market_analyst_agent", "show_range", "reference_range", "no_evidence"} <= nodes


@pytest.mark.parametrize("code, result", [
    (CEMENT_I, orchestrator.RANGE_WITH_EVIDENCE),
    (WIRE_MESH, orchestrator.RANGE_FOR_REFERENCE),
    (NONEXISTENT, orchestrator.NO_EVIDENCE),
])
def test_graph_decides_output_by_evidence(code, result, no_model):
    assert orchestrator.analyze_market(code)["result"] == result



@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def with_code(client, code, candidates=None):
    with client.session_transaction() as session:
        session["code"] = code
        session["candidates"] = candidates or [code]
        session["description"] = "cemento en bolsa"


def test_without_confirmed_code_returns_to_start(client):
    response = client.get("/mercado")
    assert response.status_code == 302 and response.headers["Location"].endswith("/")


def test_screen_shows_range_unit_awards_and_period(client, no_model):
    with_code(client, CEMENT_I)
    html = client.get("/mercado").get_data(as_text=True)
    r = market_analyst.analyze(CEMENT_I)
    for text in ("P25", "Mediana", "P75", "por unidad", format_pen(r["p25"]), format_pen(r["median"]),
                 format_pen(r["p75"]), f"{r['n_awards']:,} adjudicaciones", "2023–2025",
                 "Competitivo", "Con cautela", "Alto costo", "Ingresar mi costo"):
        assert text in html, text
    assert "Sin evidencia suficiente" not in html


def test_screen_warns_when_below_minimum(client, no_model):
    with_code(client, WIRE_MESH)
    html = client.get("/mercado").get_data(as_text=True)
    assert "Sin evidencia suficiente para un veredicto" in html
    assert f"se necesitan al menos {rules.MIN_VERDICT}" in html
    assert "Ingresar mi costo" not in html


def test_change_code_returns_to_candidates_without_calling_model(client, no_model):
    with_code(client, CEMENT_I, candidates=[12953, CEMENT_I])
    html = client.get("/candidatos").get_data(as_text=True)
    assert "CUBSO 12953" in html and f'value="{CEMENT_I}" checked' in html


def test_currency_and_unit_formats():
    assert format_pen(1234.5) == "S/ 1,234.50"
    assert format_pen(0.0456) == "S/ 0.0456"
    assert unit_text("Galon") == "galón"
    assert unit_text("M3") == "m³"
    assert unit_text("Kilogramo") == "kilogramo"