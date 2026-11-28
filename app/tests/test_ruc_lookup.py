from decimal import Decimal

from app import app
from services import accounts
from services.ruc_lookup import LookupError, RucDetails
from werkzeug.datastructures import MultiDict


RUC = "20123456789"
NAME = "Empresa ficticia consultada S.A.C."
CONSULT = "/perfil/ruc/consultar"
PROFILE = {
    "ruc": RUC, "company_name": "Empresa a mano", "business_activity": "Venta de materiales",
    "rubros": "Acero", "monthly_sales": "400.00", "experience_years": "2",
    "similar_contracts_amount": "0", "specialized_staff": "Dos técnicos",
}


def _enable(monkeypatch, lookup=None, *, daily_limit=20):
    monkeypatch.setitem(app.config, "RUC_LOOKUP_CLIENT",
                        lookup or (lambda ruc: RucDetails(ruc, NAME, "ACTIVO", "HABIDO")))
    monkeypatch.setitem(app.config, "MIAPI_RUC_DAILY_LIMIT", daily_limit)


def test_verified_company_name_survives_save_and_ruc_change(signed_in_client, account_factory, monkeypatch):
    _enable(monkeypatch)
    client = signed_in_client
    assert "data-ruc-endpoint" in client.get("/perfil").text
    result = client.post(CONSULT, data={"ruc": RUC})
    assert result.status_code == 200
    assert result.json["company_name"] == NAME
    # El nombre definitivo sale de la consulta firmada, no del mensaje del navegador.
    with client.session_transaction() as session:
        csrf_token = session["csrf_token"]
    response = client.open("/perfil", method="POST", data=MultiDict([
        *PROFILE.items(), ("csrf_token", csrf_token), ("ruc_lookup_proof", result.json["proof"]),
        ("supplier_type", "empresa"),
        ("categories", "servicios"), ("categories", "inventada"), ("categories", "bienes"),
        ("rnp_registers", "bienes"), ("rnp_registers", "consultor_obras"), ("rnp_registers", "otro"),
    ]))
    assert response.status_code == 302
    saved = accounts.get_profile(account_factory, 1)
    assert saved["company_name"] == NAME
    assert saved["supplier_type"] == "empresa"
    assert saved["categories"] == ["bienes", "servicios"]
    assert saved["rnp_registers"] == ["bienes", "consultor_obras"]
    html = client.get("/perfil").text
    assert NAME in html and "readonly" in html
    assert 'name="categories" value="servicios" checked' in html
    assert 'name="rnp_registers" value="consultor_obras" checked' in html
    assert client.post("/perfil", data=PROFILE | {"monthly_sales": "500"}).status_code == 302
    saved = accounts.get_profile(account_factory, 1)
    assert saved["company_name"] == NAME and saved["monthly_sales"] == Decimal("500")
    # Un dato consultado para el RUC anterior no se transfiere al nuevo.
    assert client.post("/perfil", data=PROFILE | {"ruc": "20123456780"}).status_code == 302
    assert accounts.get_saved_ruc_details(account_factory, 1) is None
    assert accounts.get_profile(account_factory, 1)["company_name"] == PROFILE["company_name"]


def test_unavailable_feature_and_provider_quota_keep_manual_profile(
        signed_in_client, account_factory, monkeypatch):
    client = signed_in_client
    monkeypatch.setitem(app.config, "MIAPI_RUC_TOKEN", "")
    monkeypatch.setitem(app.config, "RUC_LOOKUP_CLIENT", None)
    assert "data-ruc-endpoint" not in client.get("/perfil").text
    assert client.post(CONSULT, data={"ruc": RUC}).status_code == 404
    calls = []

    def missing(ruc):
        calls.append(ruc)
        raise LookupError(404, "Sin resultados.")

    _enable(monkeypatch, missing, daily_limit=1)
    assert client.post(CONSULT, data={"ruc": "123"}).status_code == 400
    assert client.open(CONSULT, method="POST", data={"ruc": RUC}).status_code == 400
    assert client.post(CONSULT, data={"ruc": RUC}).status_code == 404
    assert client.post(CONSULT, data={"ruc": RUC}).status_code == 429
    assert calls == [RUC]
    assert client.post("/perfil", data=PROFILE).status_code == 302
    assert accounts.get_saved_ruc_details(account_factory, 1) is None


def test_proof_is_bound_to_the_login(signed_in_client, account_factory, monkeypatch):
    _enable(monkeypatch)
    proof = signed_in_client.post(CONSULT, data={"ruc": RUC}).json["proof"]
    user = accounts.create_user(account_factory, "otra-ruc@example.test", "Clave!2026", "Otra empresa")
    client = type(signed_in_client)(app, app.response_class)
    with client.session_transaction() as session:
        session["auth_token"] = accounts.begin_session(account_factory, user)
    response = client.post("/perfil", data=PROFILE | {"ruc_lookup_proof": proof})
    assert response.status_code == 400
    assert accounts.get_saved_ruc_details(account_factory, user) is None
