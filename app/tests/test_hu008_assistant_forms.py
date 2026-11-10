"""Integración de la sesión HU-008 con los formularios del asistente."""
from html.parser import HTMLParser

from app import app
from services import accounts, assistant, tenders


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = {}
        self.action = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs.get("action")
            self.forms[self.action] = {}
        if tag == "input" and self.action and attrs.get("name"):
            self.forms[self.action][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.action = None


def test_chat_and_reset_submit_the_tokens_rendered_in_their_forms(account_factory, monkeypatch):
    # Cliente normal: no añade tokens automáticamente como FormClient.
    client = app.test_client()
    uid = accounts.create_user(account_factory, "chat@example.test", "Clave anterior de prueba", "Empresa del chat")
    with client.session_transaction() as sess:
        sess["auth_token"] = accounts.begin_session(account_factory, uid)
    monkeypatch.setattr(tenders, "availability", lambda: {"ok": False})
    calls = []

    def reply(query, history=None, profile=None):
        calls.append(query)
        return {"response": "Respuesta de integración", "profile": {"rubro": "3011"}}

    monkeypatch.setattr(assistant, "ask_assistant", reply)
    forms = Forms(client.get("/asistente").text).forms
    token = forms["/asistente"]["csrf_token"]
    assert token
    assert client.post("/asistente", data={"query": "Consulta sin token"}).status_code == 400
    assert not calls
    response = client.post("/asistente", data=forms["/asistente"] | {"query": "Vendo cemento"}, follow_redirects=True)
    assert response.status_code == 200 and "Respuesta de integración" in response.text
    assert calls == ["Vendo cemento"]
    reset = Forms(response.text).forms["/asistente/limpiar"]
    assert reset["csrf_token"] == token
    assert client.post("/asistente/limpiar", data={}).status_code == 400
    assert client.post("/asistente/limpiar", data=reset).status_code == 302
    with client.session_transaction() as sess:
        assert "assistant_history" not in sess and "supplier_profile" not in sess
        assert sess.get("auth_token")
