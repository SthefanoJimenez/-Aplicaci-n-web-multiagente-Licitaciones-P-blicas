"""RN-002: disponibilidad cuando Gemini rechaza solicitudes (429 límite de uso, 503 saturación)."""
import pytest
from google.genai import errors

import config
from agents import classifier
from app import app
from services import llm


def api_error(code):
    return errors.APIError(code, {"error": {"code": code, "message": "simulado", "status": "SIMULADO"}})


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(config, "RETRY_WAIT_SECONDS", 0)


@pytest.fixture
def calls(monkeypatch):
    """Registra a qué modelo se llamó; el modelo principal siempre responde 503."""
    made = []

    def fake_call(prompt, model, langsmith_extra=None):
        made.append(model)
        if model == "principal":
            raise api_error(503)
        return {"text": '{"ok": true}', "usage_metadata": {}}

    monkeypatch.setattr(llm, "_call_gemini", fake_call)
    return made


def test_retries_and_reports_the_code_without_fallback(calls, monkeypatch):
    monkeypatch.setattr(config, "FALLBACK_MODEL", None)
    with pytest.raises(llm.ModelError) as e:
        llm.generate_json("prompt", model="principal")
    assert e.value.code == 503
    assert calls == ["principal"] * (config.RETRIES + 1)


def test_uses_fallback_model_when_primary_is_unavailable(calls, monkeypatch):
    monkeypatch.setattr(config, "FALLBACK_MODEL", "respaldo")
    assert llm.generate_json("prompt", model="principal") == {"ok": True}
    assert calls[-1] == "respaldo"


def test_invalid_key_does_not_use_fallback(monkeypatch):
    monkeypatch.setattr(config, "FALLBACK_MODEL", "respaldo")
    made = []

    def fake_call(prompt, model, langsmith_extra=None):
        made.append(model)
        raise api_error(400)

    monkeypatch.setattr(llm, "_call_gemini", fake_call)
    with pytest.raises(llm.ModelError) as e:
        llm.generate_json("prompt", model="principal")
    assert e.value.code == 400 and made == ["principal"]


@pytest.mark.parametrize("code, text", [(503, "saturado"), (429, "límite de uso")])
def test_error_screen_explains_the_cause(monkeypatch, code, text):
    def fail(description):
        raise llm.ModelError("simulado", code=code)
    monkeypatch.setattr(classifier, "classify", fail)
    app.config["TESTING"] = True
    response = app.test_client().post("/candidatos", data={"description": "fertilizante"})
    html = response.get_data(as_text=True)
    assert response.status_code == 503
    assert text in html and 'value="fertilizante"' in html and "Reintentar búsqueda" in html