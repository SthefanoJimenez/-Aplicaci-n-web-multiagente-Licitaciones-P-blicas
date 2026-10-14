"""Pruebas de HU-003: ver la competencia por el bien (no consumen la API de Gemini).

Ejecutar desde la carpeta app/:  pytest
"""
import statistics

import pandas as pd
import pytest

from agents import classifier, market_analyst
from app import app, format_number, format_percent
from services import data, llm

CEMENT_I = 12771    
WIRE_MESH = 1414    
TRACKSUIT = 392753    
NONEXISTENT = 999999


@pytest.fixture
def no_model(monkeypatch):
    """Si algo intenta llamar al modelo de lenguaje, la prueba falla."""
    def forbidden(*args, **kwargs):
        raise AssertionError("HU-003 no debe usar el modelo de lenguaje")
    monkeypatch.setattr(llm, "_call_gemini", forbidden)
    monkeypatch.setattr(llm, "generate_json", forbidden)
    monkeypatch.setattr(classifier, "classify", forbidden)


def rows_of(code):
    table = data.AWARDS
    return table[(table["codigoitem"] == code) & (table["unidad_medida"] == data.GOODS[code]["unit"])]



def test_median_bidders_per_process_excluding_missing_data(no_model):
    r = market_analyst.analyze(CEMENT_I)
    bidders = rows_of(CEMENT_I)["n_postores"].dropna().astype(int).tolist()
    assert r["n_with_bidders"] == len(bidders) < r["n_awards"]
    assert r["median_bidders"] == statistics.median(bidders)


def test_percentage_of_single_bidder_processes(no_model):
    r = market_analyst.analyze(CEMENT_I)
    bidders = rows_of(CEMENT_I)["n_postores"].dropna().astype(int)
    assert r["n_single_bidder"] == int((bidders == 1).sum())
    assert r["pct_single_bidder"] == pytest.approx(100 * (bidders == 1).mean())


def test_awards_from_la_libertad_entities(no_model):
    r = market_analyst.analyze(CEMENT_I)
    assert r["n_la_libertad"] == int((rows_of(CEMENT_I)["entidad_departamento"] == "LA LIBERTAD").sum())


def test_hand_built_case_gives_expected_indicators(monkeypatch, no_model):
    """Caso armado a mano: 5 procesos con 1, 1, 3, 4 postores y uno sin dato."""
    rows = pd.DataFrame({
        "codigoitem": CEMENT_I, "unidad_medida": "Unidad", "anio": 2024, "codigoconvocatoria": range(5),
        "precio_unitario": [10.0, 11.0, 12.0, 13.0, 14.0],
        "n_postores": pd.array([1, 1, 3, 4, None], dtype="Int64"),
        "entidad_departamento": ["LA LIBERTAD", "LIMA", "LA LIBERTAD", "CUSCO", "LA LIBERTAD"],
    })
    monkeypatch.setattr(data, "AWARDS", rows)
    r = market_analyst.analyze(CEMENT_I)
    assert r["n_with_bidders"] == 4
    assert r["median_bidders"] == 2.0
    assert r["n_single_bidder"] == 2 and r["pct_single_bidder"] == 50.0
    assert r["n_la_libertad"] == 3


def test_good_without_bidder_data(no_model):
    r = market_analyst.analyze(TRACKSUIT)
    assert r["n_awards"] > 0 and r["n_with_bidders"] == 0
    assert r["median_bidders"] is None and r["pct_single_bidder"] is None


def test_nonexistent_good_has_no_competition(no_model):
    r = market_analyst.analyze(NONEXISTENT)
    assert r["n_with_bidders"] == 0 and r["n_la_libertad"] == 0
    assert r["median_bidders"] is None and r["pct_single_bidder"] is None



@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def market_html(client, code):
    with client.session_transaction() as session:
        session["code"] = code
        session["candidates"] = [code]
    return client.get("/mercado").get_data(as_text=True)


def test_screen_shows_the_three_indicators(client, no_model):
    r = market_analyst.analyze(CEMENT_I)
    html = market_html(client, CEMENT_I)
    for text in ("Competencia en el mercado", "Postores (mediana)", "por proceso", "Postor único",
                 "La Libertad", format_number(r["median_bidders"]), format_percent(r["pct_single_bidder"]),
                 f"{r['n_la_libertad']} fueron de entidades de La Libertad",
                 f"{r['n_awards'] - r['n_with_bidders']} adjudicaciones no tienen registro de postores"):
        assert text in html, text


def test_competition_also_shown_with_insufficient_evidence(client, no_model):
    html = market_html(client, WIRE_MESH)
    assert "Sin evidencia suficiente para un veredicto" in html
    assert "Competencia en el mercado" in html and "por proceso" in html


def test_screen_warns_when_there_is_no_bidder_data(client, no_model):
    html = market_html(client, TRACKSUIT)
    assert "no tienen registro de postores, por eso no se muestra la competencia" in html
    assert "sin datos" in html


def test_number_and_percent_formats():
    assert format_number(5.0) == "5"
    assert format_number(2.5) == "2.5"
    assert format_percent(0.36127) == "0.4%"
    assert format_percent(50.0) == "50.0%"