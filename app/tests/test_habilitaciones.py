"""Habilitaciones por grupo, no por nombre de archivo (24/11/2026).

Medición en Neon del 24/11/2026: 173 de 350 bases de bienes analizadas piden al menos una habilitación (las más
pedidas: autorización sanitaria de funcionamiento y Registro de Hidrocarburos), escritas de muchas formas. Antes
solo se reconocían si el PDF se llamaba igual; ahora la empresa dice cuál es al subirla en Mis documentos.
"""
import io
from datetime import timedelta

from services import company_documents, eligibility, licenses
from test_categorias_servicios_obras import PROFILE, TODAY, signed_in
from test_personal_fichas import text_pdf
from test_requisitos_analizados import with_facts


def license_facts(*items):
    return {"valor_estimado": {}, "consorcio": {"permitido": True},
            "requisitos": [dict({"tipo": "habilitacion", "pagina": 20, "cita": "Capacidad legal"}, **item) for item in items]}


def doc(kind=None, filename="scan_0012.pdf", days=90, document_type="habilitacion", name=None, id=1):
    return {"id": id, "filename": filename, "document_type": document_type, "valid_from": None,
            "expires_on": TODAY + timedelta(days=days), "license_kind": kind, "license_name": name}


def status(facts, documents):
    result = eligibility.evaluate(with_facts(facts), PROFILE, documents, {}, TODAY, members=[])
    check = next(c for c in result["checks"] if c.get("text"))
    return check["status"], check["reason"]


def test_las_formas_de_escribirlas_en_las_bases_caen_en_su_grupo():
    # Textos tal como salieron en la medición del 24/11/2026
    assert licenses.kinds_in("autorización e inscripción vigente en el registro de hidrocarburos del OSINERGMIN") == ["hidrocarburos"]
    assert licenses.kinds_in("registro en osinergmin") == ["hidrocarburos"]
    assert licenses.kinds_in("Resolución Directoral de autorización sanitaria de funcionamiento de establecimiento") == ["autorizacion_sanitaria"]
    assert licenses.kinds_in("registro sanitario o certificado de registro sanitario") == ["registro_sanitario"]
    assert licenses.kinds_in("certificado de buenas prácticas de almacenamiento (BPA)") == ["bpa"]
    assert licenses.kinds_in("autorización de funcionamiento vigente emitido por la DIGEMID") == ["digemid"]
    assert licenses.kinds_in("certificado de saneamiento ambiental y resolución de autorización sanitaria") == [
        "autorizacion_sanitaria", "saneamiento_ambiental"]
    assert licenses.kinds_in("Declaración jurada de cumplimiento de los términos de referencia") == []
    assert licenses.is_personal("Título profesional") and not licenses.is_personal("Registro sanitario")


def test_se_reconoce_por_lo_que_la_empresa_dijo_aunque_el_archivo_se_llame_cualquier_cosa():
    facts = license_facts({"descripcion": "Registro de Hidrocarburos",
                           "documento": "Autorización e inscripción vigente en el Registro de Hidrocarburos del OSINERGMIN"})
    assert status(facts, [doc("hidrocarburos")])[0] == "pass"
    state, reason = status(facts, [doc("hidrocarburos", days=-5)])
    assert state == "pending" and "no está vigente" in reason
    state, reason = status(facts, [doc("autorizacion_sanitaria")])
    assert state == "pending" and "Registro de Hidrocarburos de Osinergmin" in reason
    assert "Habilitación, licencia o registro" in reason


def test_los_documentos_subidos_antes_como_otro_se_siguen_reconociendo_por_el_nombre():
    facts = license_facts({"descripcion": "Registro de Hidrocarburos", "documento": "Registro de Hidrocarburos de Osinergmin"})
    assert status(facts, [doc(None, "constancia_osinergmin.pdf", document_type="otro")])[0] == "pass"


def test_si_piden_dos_habilitaciones_falta_la_que_no_tiene():
    facts = license_facts({"descripcion": "Saneamiento y autorización",
                           "documento": "Certificado de saneamiento ambiental y autorización sanitaria de funcionamiento"})
    state, reason = status(facts, [doc("saneamiento_ambiental")])
    assert state == "pending" and "Autorización sanitaria de funcionamiento" in reason and "Ya tiene" in reason
    both = [doc("saneamiento_ambiental"), doc("autorizacion_sanitaria", "b.pdf", id=2)]
    assert status(facts, both)[0] == "pass"


def test_otra_habilitacion_se_compara_por_su_nombre():
    facts = license_facts({"descripcion": "Autorización del MTC",
                           "documento": "Autorización del MTC para el transporte de mercancías"})
    other = doc(licenses.OTHER, name="Autorización MTC transporte de mercancías")
    assert status(facts, [other])[0] == "pass"
    state, reason = status(facts, [])
    assert state == "pending" and "opción «Otra»" in reason


def test_lo_que_es_del_personal_no_se_pide_en_mis_documentos():
    state, reason = status(license_facts({"descripcion": "Título profesional", "documento": "Título profesional"}), [])
    assert state == "pending" and "Personal clave" in reason


def test_para_el_contrato_se_avisa():
    facts = license_facts({"descripcion": "Registro sanitario", "documento": "Registro sanitario", "momento": "contrato"})
    state, reason = status(facts, [])
    assert state == "pending" and "firmar el contrato" in reason


def test_mis_documentos_pregunta_que_habilitacion_es(account_client, account_factory):
    uid = signed_in(account_client, account_factory)
    page = account_client.get("/documentos").get_data(as_text=True)
    assert "¿Qué habilitación es?" in page and "Registro de Hidrocarburos de Osinergmin" in page
    pdf = text_pdf(["CONSTANCIA DE REGISTRO DE HIDROCARBUROS", "Vigente"])
    base = {"document_type": "habilitacion", "no_expiry": "1"}
    missing = account_client.post("/documentos", data=dict(base, file=(io.BytesIO(pdf), "scan.pdf")))
    assert missing.status_code == 400 and "Elija qué habilitación es." in missing.get_data(as_text=True)
    other = account_client.post("/documentos", data=dict(base, license_kind="otra", file=(io.BytesIO(pdf), "scan.pdf")))
    assert other.status_code == 400 and "Escriba el nombre de la habilitación" in other.get_data(as_text=True)
    saved = account_client.post("/documentos", data=dict(base, license_kind="hidrocarburos", file=(io.BytesIO(pdf), "scan.pdf")))
    assert saved.status_code == 302
    row = company_documents.list_documents(account_factory, uid)[0]
    assert row["license_kind"] == "hidrocarburos" and row["license_label"] == "Registro de Hidrocarburos de Osinergmin"
    listing = account_client.get("/documentos").get_data(as_text=True)
    assert "Habilitación, licencia o registro: Registro de Hidrocarburos de Osinergmin" in listing
    # Se puede corregir después y, si deja de ser habilitación, se borra cuál era
    account_client.post(f"/documentos/{row['id']}/editar", data={"document_type": "otro", "no_expiry": "1",
                                                                  "license_kind": "hidrocarburos"})
    row = company_documents.list_documents(account_factory, uid)[0]
    assert row["document_type"] == "otro" and row["license_kind"] is None