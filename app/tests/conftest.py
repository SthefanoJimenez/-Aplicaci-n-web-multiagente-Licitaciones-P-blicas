"""Permite importar los módulos de la aplicación desde las pruebas."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import secrets
import pytest
from flask.testing import FlaskClient


class FormClient(FlaskClient):
    """Cliente de formularios que envía el token de la sesión como lo hace el navegador.
    Las pruebas de CSRF usan open(method='POST') para omitirlo deliberadamente.
    """
    def post(self, *args, **kwargs):
        with self.session_transaction() as sess:
            token = sess.setdefault("csrf_token", secrets.token_urlsafe(32))
        values = dict(kwargs.pop("data", {}) or {})
        values.setdefault("csrf_token", token)
        return super().post(*args, data=values, **kwargs)


@pytest.fixture
def account_factory(tmp_path, monkeypatch):
    from app import app
    from services import accounts, db
    engine = db.make_engine("sqlite:///" + str(tmp_path / "accounts.sqlite3"))
    accounts.AccountBase.metadata.create_all(engine)
    factory = db.make_session_factory(engine)
    monkeypatch.setitem(app.config, "ACCOUNTS_SESSION_FACTORY", factory)
    monkeypatch.setitem(app.config, "TESTING", True)
    yield factory
    engine.dispose()


@pytest.fixture
def account_client(account_factory):
    from app import app
    return FormClient(app, app.response_class)


@pytest.fixture
def signed_in_client(account_factory, account_client):
    """Las pruebas del flujo anterior ahora representan a un empresario autenticado."""
    from services import accounts
    uid = accounts.create_user(account_factory, "flujo@example.test", "Clave de prueba larga 2026", "Empresa de prueba")
    token = accounts.begin_session(account_factory, uid)
    with account_client.session_transaction() as sess:
        sess["auth_token"] = token
        sess["csrf_token"] = secrets.token_urlsafe(32)
    return account_client
