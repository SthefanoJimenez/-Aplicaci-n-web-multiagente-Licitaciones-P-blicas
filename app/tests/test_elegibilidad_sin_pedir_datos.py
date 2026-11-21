"""La ficha no le pide al cliente datos que debían venir de las bases (pedido del PO, 20/11/2026).

Antes, la sección «Completar datos y revisar requisitos» pedía el régimen, el RNP, la modalidad y la cobertura
de la garantía, las prestaciones accesorias, el adelanto, si era MYPE, el CUBSO y la cantidad del bien, el
personal y el aporte del socio. Si las bases no traían el dato, el cliente tampoco tenía de dónde sacarlo.
Ahora: el régimen se deduce, el RNP y la condición MYPE salen de Mi empresa, el personal de Personal clave,
el consorcio de Consorcios, y lo que la entidad no publicó (el monto) se muestra como dato no publicado,
sin cambiar el color. Solo se le pide confirmar si cumple cada requisito.
"""
from decimal import Decimal

from services import accounts, eligibility
from test_categorias_servicios_obras import (PASSWORD, PROFILE, TODAY, check, listing, public_factory,  # noqa: F401
                                             signed_in, tender_dict)

RNP_REQ = "Constancia de inscripción vigente en el Registro Nacional de Proveedores (RNP)."


def test_la_ficha_ya_no_pide_datos_de_las_bases(account_client, account_factory, listing):  # noqa: F811
    signed_in(account_client, account_factory)
    html = account_client.get("/convocatorias/1003").get_data(as_text=True)
    assert "Confirmar los requisitos" in html
    for field in ("regime", "rnp", "guarantee_method", "guarantee_capacity", "accessory_amount", "advance_amount",
                  "whole_contract", "catalog_code", "quantity", "staff_count", "partner_experience"):
        assert f'name="{field}"' not in html, field


def test_el_monto_no_publicado_no_cambia_el_color():
    result = eligibility.evaluate(tender_dict(value=None, reqs=()), PROFILE, [], {}, TODAY, members=[])
    assert check(result, "monto")["status"] == "info" and check(result, "garantia")["status"] == "info"
    assert "10 % del monto del contrato" in check(result, "garantia")["reason"]
    assert all(c["status"] in ("pass", "info") for c in result["checks"] if c["key"] in ("monto", "garantia"))


def test_con_monto_publicado_la_garantia_se_calcula_sin_preguntar():
    result = eligibility.evaluate(tender_dict(value=Decimal("800000"), reqs=()), PROFILE, [], {}, TODAY, members=[])
    guarantee = check(result, "garantia")
    assert guarantee["status"] == "pass" and result["guarantee"]["amount"] == Decimal("80000.00")


def test_mype_sale_de_mi_empresa():
    big = tender_dict(category="servicios", title="SERVICIO DE CONSULTORIA", value=Decimal("600000"), reqs=())
    without = eligibility.evaluate(big, PROFILE, [], {}, TODAY, members=[])
    assert not without["guarantee"]["retention_allowed"]
    mype = eligibility.evaluate(big, dict(PROFILE, is_mype=True), [], {}, TODAY, members=[])
    assert mype["guarantee"]["retention_allowed"] and "por ser MYPE" in check(mype, "garantia")["reason"]


def test_el_rnp_sale_de_los_registros_de_mi_empresa():
    declared = eligibility.evaluate(tender_dict(reqs=(RNP_REQ,)), PROFILE, [], {}, TODAY, members=[])
    assert "Indique sus registros" not in check(declared, "k0")["reason"]
    assert "Declara no contar" not in check(declared, "k0")["reason"]
    missing = eligibility.evaluate(tender_dict(reqs=(RNP_REQ,)), dict(PROFILE, rnp_registers=[]), [], {}, TODAY,
                                   members=[])
    assert "Indique sus registros vigentes del RNP en Mi empresa" in check(missing, "k0")["reason"]


def test_el_regimen_se_deduce_de_las_bases():
    old = eligibility.evaluate(tender_dict(value=Decimal("800000"), reqs=("Según la Ley N° 30225 y su reglamento.",)),
                               PROFILE, [], {}, TODAY, members=[])
    assert check(old, "regimen")["status"] == "pending" and check(old, "garantia")["status"] == "pending"
    new = eligibility.evaluate(tender_dict(value=Decimal("800000"), reqs=()), PROFILE, [], {}, TODAY, members=[])
    assert check(new, "regimen") is None


def test_la_condicion_mype_se_guarda_en_mi_empresa(account_client, account_factory):
    uid = signed_in(account_client, account_factory)
    account_client.post("/perfil", data={
        "company_name": "Constructora de prueba", "business_activity": "Obras", "rubros": "7214",
        "monthly_sales": "1000", "experience_years": "4", "similar_contracts_amount": "0",
        "specialized_staff": "Residente", "is_mype": "1"})
    assert accounts.get_profile(account_factory, uid)["is_mype"] is True
    assert "Mi empresa es micro o pequeña empresa (MYPE)" in account_client.get("/perfil").get_data(as_text=True)