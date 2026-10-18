"""Pruebas de HU-001 con un modelo simulado (no consumen la API de Gemini).

Ejecutar desde la carpeta app/:  pytest
"""
import pytest

import orchestrator
from agents import classifier
from app import app
from services import data, llm

CEMENT_I, CEMENT_IP, HALF_INCH_BAR, DIESEL = 12771, 12953, 130019, 233280
NONEXISTENT = 999999


def fake_model(rephrasing="", codes=(), fail_rephrase=False, fail_choose=False):
    """Imita al modelo: responde distinto según sea el paso de reformular o de elegir."""
    def call(prompt):
        if "Descripción del empresario:" in prompt:
            if fail_rephrase:
                raise llm.ModelError("simulado")
            return {"descripcion": rephrasing}
        if fail_choose:
            raise llm.ModelError("simulado")
        return {"codigos": list(codes)}
    return call



def test_validates_and_discards_nonexistent_and_repeated_codes():
    valid, discarded = classifier.validate([CEMENT_I, NONEXISTENT, CEMENT_I, "texto", CEMENT_IP])
    assert valid == [CEMENT_I, CEMENT_IP]
    assert discarded == [NONEXISTENT, "texto"]


def test_returns_up_to_three_candidates_in_order_with_code_name_and_unit():
    call = fake_model("cemento", [DIESEL, NONEXISTENT, CEMENT_I, CEMENT_IP, HALF_INCH_BAR])
    result = classifier.classify("cemento en bolsa", call_model=call)
    assert [c["code"] for c in result["candidates"]] == [DIESEL, CEMENT_I, CEMENT_IP]
    assert set(result["candidates"][0]) == {"code", "name", "unit"}
    assert result["discarded"] == [NONEXISTENT]


def test_all_candidates_exist_in_catalog():
    call = fake_model("x", [NONEXISTENT, 1, 2, CEMENT_I])
    result = classifier.classify("algo", call_model=call)
    assert all(c["code"] in data.VALID_CODES for c in result["candidates"])


def test_if_rephrasing_fails_continues_with_original_description():
    call = fake_model(codes=[CEMENT_I], fail_rephrase=True)
    result = classifier.classify("cemento portland tipo I", call_model=call)
    assert result["rephrased_description"] is None
    assert [c["code"] for c in result["candidates"]] == [CEMENT_I]


def test_if_choosing_fails_the_error_is_raised():
    with pytest.raises(llm.ModelError):
        classifier.classify("cemento", call_model=fake_model("cemento", fail_choose=True))


def test_preselection_recovers_colloquial_descriptions_thanks_to_rephrasing():
    assert HALF_INCH_BAR not in classifier.preselect("fierro corrugado de media pulgada")
    assert HALF_INCH_BAR in classifier.preselect(
        "fierro corrugado de media pulgada", "barra de acero corrugado para construccion de 1/2 in")



@pytest.mark.parametrize("description", ["", "   ", None])
def test_rejects_empty_descriptions(description):
    with pytest.raises(orchestrator.InvalidDescription):
        orchestrator.identify_good(description)


def test_rejects_too_long_descriptions():
    with pytest.raises(orchestrator.InvalidDescription):
        orchestrator.identify_good("a" * 301)


def test_without_valid_candidates_returns_no_evidence(monkeypatch):
    monkeypatch.setattr(classifier, "classify",
                        lambda d: {"candidates": [], "rephrased_description": None, "discarded": [NONEXISTENT]})
    assert orchestrator.identify_good("servicio de limpieza")["result"] == orchestrator.NO_EVIDENCE


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def fake_candidates(monkeypatch, codes):
    monkeypatch.setattr(classifier, "classify", lambda d: {
        "candidates": [data.GOODS[c] for c in codes], "rephrased_description": None, "discarded": []})


def test_description_screen(client):
    html = client.get("/evaluar").get_data(as_text=True)
    assert "¿Qué bien vende su empresa?" in html


def test_empty_description_shows_message(client):
    response = client.post("/candidatos", data={"description": "   "})
    assert response.status_code == 400
    assert "Escriba una descripción del bien" in response.get_data(as_text=True)


def test_shows_candidates_with_first_one_preselected(client, monkeypatch):
    fake_candidates(monkeypatch, [CEMENT_I, CEMENT_IP])
    html = client.post("/candidatos", data={"description": "cemento"}).get_data(as_text=True)
    assert html.count('type="radio"') == 2
    assert f'value="{CEMENT_I}" checked' in html
    assert f'value="{CEMENT_IP}" checked' not in html
    assert data.GOODS[CEMENT_I]["name"] in html


def test_confirming_a_candidate_leads_to_market(client, monkeypatch):
    fake_candidates(monkeypatch, [CEMENT_I, CEMENT_IP])
    client.post("/candidatos", data={"description": "cemento"})
    response = client.post("/confirmar", data={"code": CEMENT_IP})
    assert response.headers["Location"].endswith("/mercado")
    assert data.GOODS[CEMENT_IP]["name"] in client.get("/mercado").get_data(as_text=True)


def test_rejects_a_code_that_was_not_offered(client, monkeypatch):
    fake_candidates(monkeypatch, [CEMENT_I])
    client.post("/candidatos", data={"description": "cemento"})
    response = client.post("/confirmar", data={"code": DIESEL})
    assert response.headers["Location"].endswith("/evaluar")


def test_none_matches_returns_to_description_with_previous_text(client, monkeypatch):
    fake_candidates(monkeypatch, [CEMENT_I])
    client.post("/candidatos", data={"description": "cemento en bolsa"})
    html = client.get("/evaluar?reintentar=1").get_data(as_text=True)
    assert "Pruebe describirlo de otra forma" in html
    assert "cemento en bolsa" in html


def test_without_candidates_shows_no_evidence(client, monkeypatch):
    fake_candidates(monkeypatch, [])
    html = client.post("/candidatos", data={"description": "servicio de limpieza"}).get_data(as_text=True)
    assert "Sin evidencia para evaluar" in html


def test_if_model_fails_the_description_is_kept(client, monkeypatch):
    def fail(d):
        raise llm.ModelError("simulado")
    monkeypatch.setattr(classifier, "classify", fail)
    response = client.post("/candidatos", data={"description": "cemento en bolsa"})
    html = response.get_data(as_text=True)
    assert response.status_code == 503
    assert 'value="cemento en bolsa"' in html and "Reintentar búsqueda" in html


def test_stage_1_has_the_pipeline_nodes():
    nodes = set(orchestrator.IDENTIFICATION_STAGE.get_graph().nodes)
    assert {"classifier_agent", "show_candidates", "no_evidence"} <= nodes


def test_graph_decides_output_by_candidates(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda d: {
        "candidates": [data.GOODS[CEMENT_I]], "rephrased_description": "cemento", "discarded": []})
    state = orchestrator.IDENTIFICATION_STAGE.invoke({"description": "cemento"})
    assert state["result"] == orchestrator.CANDIDATES

    monkeypatch.setattr(classifier, "classify", lambda d: {
        "candidates": [], "rephrased_description": None, "discarded": []})
    state = orchestrator.IDENTIFICATION_STAGE.invoke({"description": "servicio de limpieza"})
    assert state["result"] == orchestrator.NO_EVIDENCE