"""Regresiones de la consulta de preparación y del formato seguro del chat."""
from html.parser import HTMLParser

import pytest

from services import assistant, tenders
from services.chat_formatting import render_assistant_text


@pytest.mark.parametrize("query", [
    "¿Qué información debo tener lista para evaluar una convocatoria?",
    "Que informacion necesito para postular?",
    "¿Qué documentos necesito preparar?",
    "¿Qué datos debo tener antes de revisar las bases?",
])
def test_preparation_question_answers_without_needing_database(query, monkeypatch):
    def unavailable():
        pytest.fail("La guía general no debe consultar convocatorias ni la base de datos")

    monkeypatch.setattr(tenders, "_session_factory", unavailable)
    result = assistant.ask_assistant(query)
    assert result["status"] == "general_guidance"
    assert result["evaluations"] == []
    for topic in ("bases", "Empresa", "Experiencia", "RNP", "vigencia", "Consorcio"):
        assert topic in result["response"]
    assert "todavía no es un veredicto" in result["response"]
    assert "necesito conocer" not in result["response"]


def test_general_guide_keeps_previously_declared_profile(monkeypatch):
    monkeypatch.setattr(tenders, "_session_factory", lambda: pytest.fail("Sin consulta de datos"))
    profile = {"rubro": "1510", "rubro_name": "Combustibles", "has_rnp": False}
    result = assistant.ask_assistant("¿Qué información debo tener lista?", profile=profile)
    assert result["profile"] == profile
    assert profile["has_rnp"] is False


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, text):
        self.text.append(text)


def test_response_formats_headings_emphasis_lists_and_paragraphs():
    result = render_assistant_text(
        "### Información\n\n**Empresa:** conserva *estos datos*.\nSegunda línea.\n\n"
        "- Primer documento\n- Segundo documento\n\n"
        "#### Próximo paso\n1. Revisar\n2. Consultar\n\n> Texto citado\n---\n`Código`"
    )
    parsed = Elements(str(result))
    tags = [tag for tag, _ in parsed.tags]
    for tag in ("h3", "h4", "strong", "em", "br", "ul", "ol", "blockquote", "hr", "code"):
        assert tag in tags
    assert tags.count("li") == 4
    assert "**" not in result and "###" not in result
    assert "Segunda línea." in parsed.text


@pytest.mark.parametrize("payload", [
    '<script>alert("x")</script>',
    '<img src=x onerror="alert(1)">',
    '**<svg onload="alert(1)">**',
    '`<iframe src="javascript:alert(1)"></iframe>`',
    '[abrir](javascript:alert(1))',
    '&lt;script&gt;alert(1)&lt;/script&gt;',
])
def test_response_content_cannot_introduce_active_html(payload):
    result = str(render_assistant_text(payload))
    parsed = Elements(result)
    assert all(tag in {"p", "strong", "code"} and not attrs for tag, attrs in parsed.tags)
    assert payload.replace("**", "").replace("`", "") in "".join(parsed.text)


def test_real_chat_renders_safe_formatted_reply(signed_in_client, monkeypatch):
    monkeypatch.setattr(tenders, "availability", lambda: {"ok": False})
    with signed_in_client.session_transaction() as session:
        session["assistant_history"] = [{
            "role": "assistant", "content": '### Guía\n- **Bases**\n- <img src=x onerror="alert(1)">'
        }]
    response = signed_in_client.get("/asistente")
    assert response.status_code == 200
    assert "<h3>Guía</h3>" in response.text
    assert "<li><strong>Bases</strong></li>" in response.text
    assert '<img src=x onerror=' not in response.text
    assert '&lt;img src=x onerror=' in response.text
