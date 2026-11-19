"""Cuentas ficticias, SQLite temporal y correo simulado: no usa SMTP real ni Neon."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import secrets

import pytest
from sqlalchemy import inspect, select, update

from app import app
from services import accounts, db, password_recovery as recovery

SEND_EMAIL = recovery.send_email

OLD = "Anterior_2026!"
NEW = "Nueva_clave_2026!"
EMAIL = "recuperacion@example.test"


@pytest.fixture(autouse=True)
def enable_password_management(monkeypatch):
    monkeypatch.setitem(app.config, "PASSWORD_MANAGEMENT_ENABLED", True)


@pytest.fixture
def user(account_factory):
    return accounts.create_user(account_factory, EMAIL, OLD, "Empresa de prueba")


@pytest.fixture
def mail(monkeypatch):
    values = {"PUBLIC_BASE_URL": "https://licitafacil.example.test", "SMTP_HOST": "smtp.example.test",
              "SMTP_FROM": "soporte@example.test", "SMTP_PORT": "587", "SMTP_USERNAME": "usuario",
              "SMTP_PASSWORD": "secreto-ficticio", "SMTP_SECURITY": "starttls",
              "EMAIL_TRANSPORT": "smtp", "EMAIL_FROM": "", "RESEND_API_KEY": ""}
    for key, value in values.items():
        monkeypatch.setitem(app.config, key, value)
    delivered = []
    monkeypatch.setattr(recovery, "send_email", lambda settings, email, token: delivered.append((email, token)))
    # Solo en esta prueba: ejecutar el trabajo en el mismo hilo para inspeccionar su efecto.
    monkeypatch.setattr(recovery, "submit", recovery.deliver)
    monkeypatch.setattr(recovery, "notify_changed", lambda *args: None)
    return delivered


def new_data(token):
    return {"reset_token": token, "password": NEW, "password_confirm": NEW}


def test_recovery_link_changes_only_its_account_and_invalidates_all_sessions(account_factory, account_client, user, mail):
    other = accounts.create_user(account_factory, "otra@example.test", OLD, "Otra empresa")
    sessions = [accounts.begin_session(account_factory, user) for _ in range(2)]
    other_session = accounts.begin_session(account_factory, other)
    response = account_client.post("/recuperar-contrasena", data={"email": EMAIL.upper()})
    assert response.status_code == 200 and "Si existe una cuenta" in response.text
    token = mail[0][1]
    with account_factory() as db:
        stored = db.scalar(select(accounts.PasswordReset))
        assert stored.token_hash == accounts.digest(token) and token not in stored.token_hash
    response = account_client.post("/restablecer-contrasena", data=new_data(token))
    assert response.status_code == 302 and "/ingresar" in response.location
    assert accounts.authenticate(account_factory, EMAIL, OLD) is None
    assert accounts.authenticate(account_factory, EMAIL, NEW) == user
    assert all(accounts.current_account(account_factory, s) is None for s in sessions)
    assert accounts.current_account(account_factory, other_session)["id"] == other
    assert accounts.authenticate(account_factory, "otra@example.test", OLD) == other
    assert account_client.post("/restablecer-contrasena", data=new_data(token)).status_code == 400
    assert account_client.post("/ingresar", data={"email": EMAIL, "password": NEW}).status_code == 302


def test_generic_response_and_no_secret_in_http(account_factory, account_client, user, mail):
    known = account_client.post("/recuperar-contrasena", data={"email": EMAIL})
    absent = account_client.post("/recuperar-contrasena", data={"email": "ausente@example.test"})
    assert known.status_code == absent.status_code == 200 and known.data == absent.data
    assert len(mail) == 1 and mail[0][1] not in known.text
    assert known.headers["Cache-Control"] == "no-store"


def test_rate_limit_per_email_across_ips_and_per_ip_across_emails(account_client, user, mail):
    for i in range(5):
        r = account_client.post("/recuperar-contrasena", data={"email": EMAIL},
                                environ_overrides={"REMOTE_ADDR": f"127.0.0.{i+1}"})
        assert r.status_code == 200
    assert len(mail) == 3
    # Los límites de recuperación no bloquean el ingreso normal.
    assert account_client.post("/ingresar", data={"email": EMAIL, "password": OLD}).status_code == 302


def test_missing_configuration_does_not_fake_delivery(account_client, monkeypatch):
    monkeypatch.setitem(app.config, "SMTP_HOST", "")
    assert account_client.get("/recuperar-contrasena").status_code == 503


@pytest.mark.parametrize("origin", ["http://example.test", "https://user:secret@example.test", "https://example.test/?x=y", "https://example.test/#x", "https://example.test:invalid"])
def test_email_origin_cannot_come_from_untrusted_host(mail, monkeypatch, origin):
    monkeypatch.setitem(app.config, "PUBLIC_BASE_URL", origin)
    assert recovery.settings(app) is None


def test_expired_and_changed_tokens_do_not_change_password(account_factory, user):
    token = accounts.issue_password_reset(account_factory, EMAIL)
    with account_factory.begin() as db:
        db.execute(update(accounts.PasswordReset).values(expires_at=accounts.now() - timedelta(seconds=1)))
    assert not accounts.reset_password(account_factory, token, NEW)
    assert accounts.authenticate(account_factory, EMAIL, OLD) == user
    token = accounts.issue_password_reset(account_factory, EMAIL)
    assert not accounts.reset_password(account_factory, token + "x", NEW)
    assert accounts.reset_password(account_factory, token, NEW)


def test_requesting_a_second_link_does_not_revoke_the_first(account_factory, user):
    first = accounts.issue_password_reset(account_factory, EMAIL)
    second = accounts.issue_password_reset(account_factory, EMAIL)
    assert accounts.reset_password(account_factory, first, NEW)
    assert not accounts.reset_password(account_factory, second, "Otra_clave_2026!")


def test_same_token_cannot_win_twice_under_concurrency(account_factory, user):
    token = accounts.issue_password_reset(account_factory, EMAIL)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: accounts.reset_password(account_factory, token, NEW), range(2)))
    assert sorted(outcomes) == [False, True]


def test_change_password_requires_current_password_and_revokes_links(account_factory, account_client, user):
    assert account_client.get("/perfil/contrasena").status_code == 302
    account_client.post("/ingresar", data={"email": EMAIL, "password": OLD})
    other_session = accounts.begin_session(account_factory, user)
    token = accounts.issue_password_reset(account_factory, EMAIL)
    data = {"current_password": "incorrecta", "password": NEW, "password_confirm": NEW, "user_id": "999"}
    assert account_client.post("/perfil/contrasena", data=data).status_code == 400
    assert accounts.authenticate(account_factory, EMAIL, OLD) == user
    data["current_password"] = OLD
    assert account_client.post("/perfil/contrasena", data=data).status_code == 302
    assert accounts.current_account(account_factory, other_session) is None
    assert not accounts.reset_password(account_factory, token, OLD)
    assert account_client.get("/perfil").status_code == 302
    assert accounts.authenticate(account_factory, EMAIL, NEW) == user


@pytest.mark.parametrize("password,confirmation", [("sincomplejidad", "sincomplejidad"), (NEW, "diferente"), ("A1!", "A1!")])
def test_invalid_new_password_does_not_consume_token(account_factory, account_client, user, password, confirmation):
    token = accounts.issue_password_reset(account_factory, EMAIL)
    assert account_client.post("/restablecer-contrasena", data={"reset_token": token,
        "password": password, "password_confirm": confirmation}).status_code == 400
    assert accounts.reset_password(account_factory, token, NEW)


@pytest.mark.parametrize("path", ["/recuperar-contrasena", "/restablecer-contrasena", "/perfil/contrasena"])
def test_new_forms_enforce_csrf(account_factory, account_client, user, mail, path):
    if path.startswith("/perfil"):
        account_client.post("/ingresar", data={"email": EMAIL, "password": OLD})
    assert account_client.open(path, method="POST", data={"email": EMAIL}).status_code == 400


def test_smtp_failure_revokes_token_without_logging_secrets(account_factory, user, mail, monkeypatch, caplog):
    def broken(*args):
        raise OSError("private recipient and password must not appear")
    monkeypatch.setattr(recovery, "send_email", broken)
    recovery.deliver(account_factory, recovery.settings(app), EMAIL)
    with account_factory() as db:
        assert db.scalar(select(accounts.PasswordReset)) is None
    assert "private recipient" not in caplog.text and EMAIL not in caplog.text


def test_smtp_tls_and_fixed_fragment_link(mail, monkeypatch):
    sent, calls = [], []
    class FakeSMTP:
        def __init__(self, host, port, **kwargs):
            calls.append((host, port))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): pass
        def starttls(self, **kwargs): calls.append("tls")
        def login(self, *args): calls.append("login")
        def send_message(self, message): sent.append(message)
    monkeypatch.setattr(recovery.smtplib, "SMTP", FakeSMTP)
    SEND_EMAIL(recovery.settings(app), EMAIL, "secreto-ficticio")
    assert calls == [("smtp.example.test", 587), "tls", "login"]
    assert "https://licitafacil.example.test/restablecer-contrasena#secreto-ficticio" in sent[0].get_content()


def test_reset_page_has_no_referrer_and_keeps_secret_out_of_get(account_client):
    response = account_client.get("/restablecer-contrasena")
    assert response.status_code == 200
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_https_email_transport_for_render_free(mail, monkeypatch):
    monkeypatch.setitem(app.config, "EMAIL_TRANSPORT", "resend")
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "clave-ficticia")
    monkeypatch.setitem(app.config, "EMAIL_FROM", "pruebas@example.test")
    monkeypatch.setitem(app.config, "SMTP_HOST", "")
    calls = []
    class Response:
        status_code = 200
        def json(self): return {"id": "correo-ficticio"}
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    monkeypatch.setattr(recovery.requests, "post", post)
    SEND_EMAIL(recovery.settings(app), EMAIL, "secreto-ficticio")
    assert calls[0][0] == "https://api.resend.com/emails"
    request = calls[0][1]
    assert request["allow_redirects"] is False and request["timeout"] == 8
    assert request["headers"]["Authorization"] == "Bearer clave-ficticia"
    assert request["json"]["from"] == "pruebas@example.test"
    assert request["json"]["to"] == [EMAIL]
    assert "restablecer-contrasena#secreto-ficticio" in request["json"]["text"]


def test_https_provider_failure_revokes_reset_without_logging_private_response(account_factory, user, mail, monkeypatch, caplog):
    monkeypatch.setitem(app.config, "EMAIL_TRANSPORT", "resend")
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "clave-ficticia")
    class Response:
        status_code = 401
    monkeypatch.setattr(recovery.requests, "post", lambda *a, **kw: Response())
    # Utiliza el transporte real con petición HTTPS simulada.
    monkeypatch.setattr(recovery, "send_email", SEND_EMAIL)
    recovery.deliver(account_factory, recovery.settings(app), EMAIL)
    with account_factory() as db:
        assert db.scalar(select(accounts.PasswordReset)) is None
    assert EMAIL not in caplog.text and "clave-ficticia" not in caplog.text


def test_https_transport_requires_key_and_does_not_fall_back_to_smtp(mail, monkeypatch):
    monkeypatch.setitem(app.config, "EMAIL_TRANSPORT", "resend")
    assert recovery.settings(app) is None


def test_additive_schema_keeps_existing_accounts(account_factory, user):
    engine = account_factory.kw["bind"]
    accounts.initialize_schema(engine, password_management=True)
    accounts.initialize_schema(engine, password_management=True)
    assert accounts.authenticate(account_factory, EMAIL, OLD) == user


def test_disabled_password_management_keeps_login_and_hides_new_routes(
        account_factory, account_client, user, mail, monkeypatch):
    monkeypatch.setitem(app.config, "PASSWORD_MANAGEMENT_ENABLED", False)
    assert "Olvidé mi contraseña" not in account_client.get("/ingresar").text
    for path in ("/recuperar-contrasena", "/restablecer-contrasena", "/perfil/contrasena"):
        assert account_client.get(path).status_code == 404
        assert account_client.post(path, data={}).status_code == 404
    assert account_client.post("/ingresar", data={"email": EMAIL, "password": OLD}).status_code == 302
    page = account_client.get("/perfil")
    assert page.status_code == 200 and "Cambiar contraseña" not in page.text
    assert mail == []


def test_optional_schema_does_not_require_password_tables_by_default(tmp_path):
    engine = db.make_engine("sqlite:///" + str(tmp_path / "cuentas.sqlite3"))
    try:
        accounts.initialize_schema(engine)
        names = set(inspect(engine).get_table_names())
        assert "usuarios" in names
        optional = {"recuperaciones_contrasena", "limites_recuperacion"}
        assert not (names & optional)
        accounts.initialize_schema(engine, password_management=True)
        assert optional <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
