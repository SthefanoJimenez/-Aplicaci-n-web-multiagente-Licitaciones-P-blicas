"""Regresión de la ficha 1265895: un reglamento técnico no exige contratar técnicos."""
from datetime import date

import pytest

from services import eligibility


def evaluate_requirement(text):
    tender = {
        "code": "1265895", "title": "Compra de cemento", "state": "vigente",
        "deadline_at": None, "estimated_value": None, "currency": "PEN", "items": [],
        "requirements_status": "extraido", "requirements": [{
            "key": "requisito-pagina-34", "text": text, "page": 34, "file_name": "bases.pdf",
        }],
    }
    profile = {"company_name": "Empresa de prueba", "business_activity": "Venta de zapatos",
               "rubros": "Zapatos", "specialized_staff": "No cuento con personal especializado"}
    return eligibility.evaluate(tender, profile, [], today=date(2026, 11, 14))


@pytest.mark.parametrize("reference", ["Reglamento Técnico", "Reglamentos Técnicos", "Reglamento\nTécnico"])
def test_referencia_a_reglamento_tecnico_no_inventa_documentos_de_personal(reference):
    result = evaluate_requirement(
        "A. CAPACIDAD LEGAL. Contar con copia simple del Certificado de Conformidad "
        "presentado ante el Ministerio de la Producción, conforme a los artículos 9 y 10 "
        f"del {reference} sobre Cemento Hidráulico utilizado en Edificaciones y "
        "Construcciones en General. El postor deberá acreditar con copia simple del certificado.")
    requirement = next(c for c in result["checks"] if c["key"] == "requisito-pagina-34")
    assert "Documentos de Personal" not in requirement["reason"]
    assert "cantidad de personal" not in requirement["reason"]
    assert "Documentos de Otro ausentes" in requirement["reason"]
    assert requirement["status"] == "pending" and result["color"] == "amarillo"


def test_reglamento_tecnico_no_oculta_un_requisito_real_de_personal():
    result = evaluate_requirement(
        "Cumplir el Reglamento Técnico del cemento. Personal: acreditar 2 técnicos especializados.")
    requirement = next(c for c in result["checks"] if c["key"] == "requisito-pagina-34")
    assert "Documentos de Personal ausentes" in requirement["reason"]
    assert "cantidad de personal" in requirement["reason"]
