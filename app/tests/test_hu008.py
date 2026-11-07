"""HU-008: registro, persistencia del perfil, separación de empresas y control de acceso.
SQLite temporal; no se usan claves, APIs, Neon ni datos de empresas reales.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from werkzeug.security import check_password_hash

from app import app
from services import accounts

PASSWORD = "Una frase de prueba 2026"
PROFILE = {"company_name": "Materiales del Norte", "business_activity": "Venta de materiales",
           "rubros": "Cemento y acero", "monthly_sales": "15000.50", "experience_years": "5",
           "similar_contracts_amount": "80000.00", "specialized_staff": "Un técnico con tres años de experiencia"}


def register(client, email="arturo@example.test", **extra):
    return client.post("/registro", data={"email": email, "company_name": "Empresa de prueba",
                                         "password": PASSWORD, "password_confirm": PASSWORD, **extra})


def test_register_creates_company_hash_and_session(account_client, account_factory):
    response = register(account_client, "  ARTURO@EXAMPLE.TEST  ")
    assert response.status_code == 302 and response.location.endswith("/perfil")
    with account_factory() as db:
        user = db.scalar(select(accounts.User))
        company = db.scalar(select(accounts.Company))
        assert user.email == "arturo@example.test"
        assert company.user_id == user.id
        assert user.password_hash != PASSWORD
        assert check_password_hash(user.password_hash, PASSWORD)
    with account_client.session_transaction() as sess:
        assert "auth_token" in sess and PASSWORD not in str(dict(sess))
    assert "Mi empresa" in account_client.get("/perfil").text


def test_profile_survives_logout_login_and_new_browser(account_client, account_factory):
    register(account_client)
    assert account_client.post("/perfil", data=PROFILE).status_code == 302
    with account_factory() as db:
        p = db.scalar(select(accounts.CompanyProfile))
        assert p.monthly_sales == Decimal("15000.50")
        assert p.similar_contracts_amount == Decimal("80000.00")
        assert p.experience_years == 5
    assert account_client.post("/salir").status_code == 302
    other_browser = type(account_client)(app, app.response_class)
    assert other_browser.post("/ingresar", data={"email": "arturo@example.test", "password": PASSWORD}).status_code == 302
    html = other_browser.get("/perfil").text
    for expected in ("Materiales del Norte", "Cemento y acero", "15000.50", "80000.00", PROFILE["specialized_staff"]):
        assert expected in html
    changed = PROFILE | {"monthly_sales": "17000,25", "specialized_staff": "Dos técnicos"}
    other_browser.post("/perfil", data=changed)
    assert "17000.25" in other_browser.get("/perfil").text
    with other_browser.session_transaction() as sess:
        assert not any(key in sess for key in PROFILE)  # El perfil no viaja en la cookie.


@pytest.mark.parametrize("path", ["/evaluar", "/candidatos", "/mercado", "/costo", "/veredicto", "/perfil", "/asistente", "/documentos", "/consorcio"])
def test_private_urls_require_login(account_client, path):
    response = account_client.get(path)
    assert response.status_code == 302 and "/ingresar?next=" in response.location


@pytest.mark.parametrize("path", ["/", "/panorama", "/datos", "/convocatorias"])
def test_public_urls_remain_public(account_client, path, monkeypatch):
    from services import tenders
    monkeypatch.setattr(tenders, "availability", lambda: {"ok": False, "reason": "sin_configurar"})
    assert account_client.get(path).status_code == 200


def test_private_post_does_not_run_classifier_without_login(account_client, monkeypatch):
    import orchestrator
    def forbidden(*args, **kwargs):
        raise AssertionError("No debe llamar al agente sin sesión")
    monkeypatch.setattr(orchestrator, "identify_good", forbidden)
    assert account_client.post("/candidatos", data={"description": "cemento"}).status_code == 302


def test_duplicate_email_wrong_password_and_logout(account_client, account_factory):
    register(account_client)
    cookie = account_client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    account_client.post("/salir")
    assert account_client.get("/perfil").status_code == 302
    # Una copia anterior de la cookie tampoco recupera una sesión cerrada.
    stolen = type(account_client)(app, app.response_class)
    stolen.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie)
    assert stolen.get("/perfil").status_code == 302
    assert register(account_client, email="ARTURO@example.test").status_code == 400
    assert account_client.post("/ingresar", data={"email": "arturo@example.test", "password": "incorrecta"}).status_code == 400
    assert account_client.get("/perfil").status_code == 302
    with account_factory() as db:
        assert len(db.scalars(select(accounts.User)).all()) == 1


def test_two_companies_cannot_read_or_overwrite_each_other(account_client, account_factory):
    register(account_client)
    account_client.post("/perfil", data=PROFILE)
    account_client.post("/salir")
    register(account_client, email="otra@example.test")
    assert "15000.50" not in account_client.get("/perfil?company_id=1&user_id=1").text
    account_client.post("/perfil", data=PROFILE | {"monthly_sales": "42", "company_name": "Otra empresa", "company_id": "1", "user_id": "1"})
    with account_factory() as db:
        first = db.scalar(select(accounts.CompanyProfile).where(accounts.CompanyProfile.company_id == 1))
        second = db.scalar(select(accounts.CompanyProfile).where(accounts.CompanyProfile.company_id == 2))
        assert first.monthly_sales == Decimal("15000.50") and second.monthly_sales == Decimal("42")


@pytest.mark.parametrize("field,value", [("monthly_sales", "-1"), ("monthly_sales", "NaN"),
                                        ("monthly_sales", "Infinity"), ("similar_contracts_amount", "1e30"),
                                        ("monthly_sales", "12.345"), ("experience_years", "-2"),
                                        ("experience_years", "abc"), ("business_activity", ""), ("rubros", "x" * 501)])
def test_invalid_profile_is_rejected_without_partial_save(account_client, account_factory, field, value):
    register(account_client)
    account_client.post("/perfil", data=PROFILE)
    response = account_client.post("/perfil", data=PROFILE | {field: value, "company_name": "No guardar"})
    assert response.status_code == 400 and 'role="alert"' in response.text
    assert accounts.get_profile(account_factory, 1)["company_name"] == PROFILE["company_name"]


def test_csrf_and_logout_require_post(account_client):
    assert account_client.open("/registro", method="POST", data={}).status_code == 400
    register(account_client)
    assert account_client.open("/perfil", method="POST", data=PROFILE).status_code == 400
    assert account_client.open("/salir", method="POST", data={}).status_code == 400
    assert account_client.open("/perfil", method="POST", data=PROFILE | {"csrf_token": "sesión ajena"}).status_code == 400
    assert account_client.get("/salir").status_code == 405
    assert account_client.get("/perfil").status_code == 200


@pytest.mark.parametrize("target", ["https://example.org/", "//example.org/", "/\\example.org/", "/\nexample.org/", "/%2fexample.org/", "/%5cexample.org/"])
def test_login_does_not_redirect_outside_application(account_client, target):
    register(account_client)
    account_client.post("/salir")
    response = account_client.post("/ingresar", data={"email": "arturo@example.test", "password": PASSWORD, "next": target})
    assert response.location.endswith("/perfil")


def test_successful_login_returns_to_requested_private_page(account_client):
    register(account_client)
    account_client.post("/salir")
    response = account_client.post("/ingresar", data={"email": "arturo@example.test", "password": PASSWORD, "next": "/evaluar?reintentar=1"})
    assert response.location == "/evaluar?reintentar=1"


def test_expired_session_and_login_throttle(account_client, account_factory):
    register(account_client)
    with account_factory.begin() as db:
        row = db.scalar(select(accounts.LoginSession))
        row.expires_at = accounts.now() - timedelta(seconds=1)
    assert account_client.get("/perfil").status_code == 302
    for _ in range(8):
        assert account_client.post("/ingresar", data={"email": "arturo@example.test", "password": "incorrecta"}).status_code == 400
    assert account_client.post("/ingresar", data={"email": "arturo@example.test", "password": PASSWORD}).status_code == 429


def test_navigation_and_no_cache_for_private_profile(account_client):
    assert "Crear cuenta" in account_client.get("/").text
    register(account_client)
    response = account_client.get("/perfil")
    assert response.headers["Cache-Control"] == "no-store"
    assert "Cerrar sesión" in response.text and "Crear cuenta" not in response.text


def test_database_failure_is_explained_without_leaking_details(account_client, monkeypatch):
    import auth
    from sqlalchemy.exc import SQLAlchemyError
    def fail():
        raise SQLAlchemyError("PRIVATE_DATABASE_PASSWORD")
    monkeypatch.setattr(auth, "store", fail)
    response = account_client.post("/ingresar", data={"email": "prueba@example.test", "password": PASSWORD})
    assert response.status_code == 503 and "PRIVATE_DATABASE_PASSWORD" not in response.text
