"""Pasos 4 y 5: costo del usuario, veredicto (HU-005) y explicación (HU-006)."""
import pytest

import rules
from agents import classifier, evaluator, market_analyst, writer
from app import app
from services import data, llm

CEMENT_I = 12771      # cemento portland tipo I, con veredicto y confianza alta
WIRE_MESH = 1414      # entre MIN_CATALOG y MIN_VERDICT: sin veredicto


@pytest.fixture
def no_model(monkeypatch):
    """El veredicto y la explicación no deben llamar al modelo de lenguaje."""
    def forbidden(*args, **kwargs):
        raise AssertionError("Los pasos 4 y 5 no deben usar el modelo de lenguaje")
    monkeypatch.setattr(llm, "_call_gemini", forbidden)
    monkeypatch.setattr(llm, "generate_json", forbidden)
    monkeypatch.setattr(classifier, "classify", forbidden)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def with_code(client, code):
    with client.session_transaction() as session:
        session["code"] = code
        session["candidates"] = [code]
        session["description"] = data.GOODS[code]["name"]


# ----------------------------------------------------------------- Lectura del costo
@pytest.mark.parametrize("text, expected", [
    ("30.5", 30.5), ("30,50", 30.5), ("12,503", 12.503), ("1,234.50", 1234.5), ("S/ 28.10", 28.1), (" 7 ", 7.0),
])
def test_parse_cost_accepts_common_formats(text, expected):
    assert evaluator.parse_cost(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "   ", "abc", "0", "-5", "1e12", "12..5"])
def test_parse_cost_rejects_invalid_values(text):
    with pytest.raises(evaluator.InvalidCost):
        evaluator.parse_cost(text)


# ----------------------------------------------------------------- Veredicto
def test_verdict_follows_sp002_table(no_model):
    m = market_analyst.analyze(CEMENT_I)
    assert evaluator.evaluate(m["p25"] - 0.01, m)["verdict"] == rules.PARTICIPATE
    assert evaluator.evaluate(m["p25"], m)["verdict"] == rules.PARTICIPATE_WITH_CAUTION
    assert evaluator.evaluate(m["p75"], m)["verdict"] == rules.PARTICIPATE_WITH_CAUTION
    assert evaluator.evaluate(m["p75"] + 0.01, m)["verdict"] == rules.NOT_PROFITABLE


def test_verdict_includes_confidence_and_marker(no_model):
    m = market_analyst.analyze(CEMENT_I)
    e = evaluator.evaluate(m["median"], m)
    assert e["confidence"] == rules.confidence_level(m["n_awards"], m["dispersion"])
    assert e["confidence_label"] in rules.CONFIDENCE_LABELS.values()
    assert 25 <= e["marker_pct"] <= 75
    assert e["vs_median_pct"] == pytest.approx(0)
    assert 2 <= evaluator.evaluate(0.01, m)["marker_pct"] < 25
    assert 75 < evaluator.evaluate(m["p75"] * 10, m)["marker_pct"] <= 98


def test_evaluator_is_not_traced():
    """El costo no debe registrarse en LangSmith: el evaluador no usa @traceable."""
    source = open(evaluator.__file__, encoding="utf-8").read()
    assert "traceable(" not in source and "langsmith" not in source


# ----------------------------------------------------------------- Explicación
def test_explanation_mentions_verdict_cases_and_confidence(no_model):
    m = market_analyst.analyze(CEMENT_I)
    d = market_analyst.describe(CEMENT_I, m["unit"])
    for cost in (m["p25"] * 0.9, m["median"], m["p75"] * 1.1):
        e = evaluator.evaluate(cost, m)
        w = writer.explain(e, m, d)
        text = " ".join([w["title"], w["text"], *w["reasons"]])
        assert f"{m['n_awards']:,} compras" in text
        assert "Confianza" in text
        assert w["places"], "debe sugerir departamentos donde buscar"
    assert any(p["local"] for p in w["places"]), "La Libertad debe aparecer si compró el bien"


# ----------------------------------------------------------------- Pantallas
def test_cost_screen_requires_evidence(client, no_model):
    with_code(client, WIRE_MESH)
    assert client.get("/costo").headers["Location"].endswith("/mercado")
    with_code(client, CEMENT_I)
    html = client.get("/costo").get_data(as_text=True)
    assert "Su costo por unidad" in html and "no se guarda" in html


def test_verdict_screen_shows_result_and_does_not_store_cost(client, no_model):
    with_code(client, CEMENT_I)
    response = client.post("/veredicto", data={"cost": "29.50"})
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Participa con cautela" in html
    assert "Nivel de confianza" in html
    assert "Dónde buscar convocatorias" in html
    with client.session_transaction() as session:
        assert "29.5" not in str(dict(session))
        assert not any("cost" in key for key in session)


def test_invalid_cost_shows_error(client, no_model):
    with_code(client, CEMENT_I)
    response = client.post("/veredicto", data={"cost": "abc"})
    assert response.status_code == 400
    assert "Escriba solo el número" in response.get_data(as_text=True)


def test_verdict_without_evidence_returns_to_start(client, no_model):
    with_code(client, WIRE_MESH)
    response = client.post("/veredicto", data={"cost": "10"})
    assert response.status_code == 302
    assert client.get("/veredicto").status_code == 302


# ----------------------------------------------------------------- RNP (HU-004 y HU-005)
def test_cost_screen_asks_rnp_and_what_to_include(client, no_model):
    with_code(client, CEMENT_I)
    html = client.get("/costo").get_data(as_text=True)
    assert "RNP" in html and 'name="rnp"' in html
    assert "flete" in html and "IGV" in html


def test_rnp_does_not_block_and_warns_when_missing(client, no_model):
    with_code(client, CEMENT_I)
    without_answer = client.post("/veredicto", data={"cost": "29.50"}).get_data(as_text=True)
    assert "Participa con cautela" in without_answer and "Sin RNP vigente" not in without_answer
    with_rnp = client.post("/veredicto", data={"cost": "29.50", "rnp": "si"}).get_data(as_text=True)
    assert "Registro Nacional de Proveedores" not in with_rnp
    no_rnp = client.post("/veredicto", data={"cost": "29.50", "rnp": "no"}).get_data(as_text=True)
    assert "Sin RNP vigente no puede presentar ofertas" in no_rnp
    with client.session_transaction() as session:
        assert "rnp" not in session


def test_market_explains_how_to_read_the_result(client, no_model):
    with_code(client, CEMENT_I)
    html = client.get("/mercado").get_data(as_text=True)
    assert "Cómo leer el resultado" in html


def test_cost_field_accepts_only_numbers(client, no_model):
    with_code(client, CEMENT_I)
    html = client.get("/costo").get_data(as_text=True)
    assert 'data-numeric' in html and 'inputmode="decimal"' in html and 'pattern="[0-9]+' in html


def test_verdict_warns_when_market_prices_are_out_of_reach(client, no_model):
    with_code(client, CEMENT_I)
    m = market_analyst.analyze(CEMENT_I)
    html = client.post("/veredicto", data={"cost": f"{m['p75'] * 3:.2f}"}).get_data(as_text=True)
    assert "Su costo está fuera del rango en que compra el Estado" in html
    assert "El Estado pagó menos que su costo en el" in html
    ok = client.post("/veredicto", data={"cost": f"{m['p25'] * 0.9:.2f}"}).get_data(as_text=True)
    assert "fuera del rango" not in ok