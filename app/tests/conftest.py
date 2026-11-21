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


@pytest.fixture(autouse=True)
def _configuracion_de_pruebas(monkeypatch):
    """Las pruebas no dependen del .env de cada equipo (20/11/2026).

    - Rubros: desde el 20/11/2026 la ingesta guarda todos por defecto (RUBROS_INGESTA=todos). Las pruebas de
      EN-007, EN-011 y HU-012 cuentan con el modo de piloto y solicitados; las que prueban «todos» lo activan
      con monkeypatch.setattr(config, "ALL_RUBROS", True).
    - Lectura de bases: los valores por defecto (25 bases, 60 cronogramas, una a la vez), aunque el .env diga
      RAG_MAX_BASES_PER_RUN=0, CRONOGRAMA_MAX_POR_EJECUCION=0 o RAG_HILOS=6. Las pruebas en paralelo pasan
      workers explícitamente (SQLite en memoria no se comparte entre hilos).
    """
    import config
    monkeypatch.setattr(config, "ALL_RUBROS", False)
    monkeypatch.setattr(config, "CATEGORIES", ("bienes", "servicios", "obras"))
    monkeypatch.setattr(config, "RAG_MAX_BASES_PER_RUN", 25)
    monkeypatch.setattr(config, "SCHEDULE_MAX_PER_RUN", 60)
    monkeypatch.setattr(config, "RAG_WORKERS", 1)
    # 20/11/2026: las pruebas nunca leen con Gemini (el .env de cada equipo puede tener la clave)
    monkeypatch.setattr(config, "OCR_ENGINE", "tesseract")
    # Ni el análisis de requisitos con Gemini (las pruebas que lo usan pasan su propia función)
    monkeypatch.setattr(config, "BASES_ANALYSIS", False)
    # Sin las esperas de Gemini (hasta 3,5 min): las pruebas que las necesitan las fijan en 0 s.
    from services import rag
    monkeypatch.setattr(rag, "EMBED_WAITS", ())
    monkeypatch.setattr(rag, "_PACE", rag._GeminiPace())
    monkeypatch.setattr(rag, "_TEXT_PACE", rag._GeminiPace(per_minute=0))
    monkeypatch.setattr(rag, "ANALYSIS_WAITS", ())
    monkeypatch.setattr("ingest_job.RAG_STOPPED", False)
    # Cuota de Gemini «agotada» en una prueba no pasa a la siguiente
    from services import bases_reader, llm
    llm.reset_exhausted()
    bases_reader._ocr_failures["seguidos"] = 0
    yield
    llm.reset_exhausted()


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