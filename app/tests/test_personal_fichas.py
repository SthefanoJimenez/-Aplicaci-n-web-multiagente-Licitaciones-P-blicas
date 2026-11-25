"""Fichas técnicas y sustentos del personal clave (pedido del asesor, 20/11/2026).

En Mi empresa › Personal clave, a cada persona se le suben su currículum o ficha técnica, título, constancia
de colegiatura y habilidad, constancias de experiencia y certificados de capacitación. Se lee su texto (sin
modelos de lenguaje) para ver si mencionan la profesión declarada y, en la constancia, la colegiatura y su
vigencia. El semáforo pide el sustento de quien cubre cada cargo de las bases.
"""
import io
from pathlib import Path
from datetime import date

import pytest
from sqlalchemy import func, select

from services import accounts, company_documents, eligibility, staff
from test_categorias_servicios_obras import PASSWORD, PROFILE, TODAY, check, member, signed_in, tender_dict


def text_pdf(lines):
    """PDF mínimo de una página con texto (Helvetica), sin dependencias."""
    def esc(text):
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    body = "BT /F1 11 Tf 50 800 Td 14 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    stream = body.encode("latin-1")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> "
               b"/Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


LICENSE = text_pdf(["CONSTANCIA DE HABILIDAD", "El ingeniero JUAN PEREZ, INGENIERO CIVIL, Registro CIP N 123456,",
                    "se encuentra HABILITADO para el ejercicio profesional. Vigente hasta el 31/12/2026."])
CV_CIVIL = text_pdf(["CURRICULUM VITAE - JUAN PEREZ", "Ingeniero Civil colegiado. Residente de obra en pistas y veredas",
                     "durante 36 meses en municipalidades de La Libertad."])
CV_OTHER = text_pdf(["CURRICULUM VITAE - JUAN PEREZ", "Ingeniero de Sistemas con experiencia en redes y servidores",
                     "y desarrollo de aplicaciones durante varios años."])


def add_person(client, factory):
    uid = signed_in(client, factory)
    client.post("/perfil/personal", data={"name": "Juan Pérez", "profession": "Ingeniero civil", "years": "3",
                                          "months": "0", "licensed": "1"})
    return uid, staff.list_staff(factory, uid)[0]["id"]


def upload(client, member_id, content, kind, filename="documento.pdf", **extra):
    return client.post(f"/perfil/personal/{member_id}/documentos",
                       data={"kind": kind, **extra, "file": (io.BytesIO(content), filename)})


def test_se_sube_la_constancia_y_se_lee_colegiatura_habilidad_y_vigencia(account_client, account_factory):
    uid, member_id = add_person(account_client, account_factory)
    response = upload(account_client, member_id, LICENSE, "colegiatura", "habilidad.pdf")
    assert response.status_code == 302 and response.headers["Location"].endswith(f"#persona-{member_id}")
    person = staff.list_staff(account_factory, uid)[0]
    doc = person["documents"][0]
    assert doc["document_type"] == "personal" and doc["staff_kind"] == "colegiatura"
    assert doc["expires_on"] == date(2026, 12, 31)                       # se tomó de la constancia
    assert "Colegiatura N.° 123456" in doc["read_detail"] and "habilitado" in doc["read_detail"]
    assert person["evidence"]["kinds"] == ["colegiatura"]
    html = account_client.get("/perfil/personal").get_data(as_text=True)
    assert "Personal clave y fichas técnicas" in html and "habilidad.pdf" in html and "Colegiatura N.° 123456" in html
    assert "Constancia de colegiatura y habilidad ✓" in html and "Título profesional —" in html
    documents = account_client.get("/documentos").get_data(as_text=True)
    assert "Constancia de colegiatura y habilidad" in documents and f"#persona-{member_id}" in documents


def test_un_curriculum_de_otra_profesion_queda_observado(account_client, account_factory):
    uid, member_id = add_person(account_client, account_factory)
    upload(account_client, member_id, CV_OTHER, "cv", "cv.pdf")
    doc = staff.list_staff(account_factory, uid)[0]["documents"][0]
    assert doc["read_status"] == "no_coincide"
    assert doc["read_detail"] == "No menciona ingeniero civil; menciona ingeniero de sistemas."


def test_un_pdf_sin_texto_se_guarda_igual(account_client, account_factory):
    from test_hu015 import pdf_bytes
    uid, member_id = add_person(account_client, account_factory)
    upload(account_client, member_id, pdf_bytes(label="escaneado"), "titulo")
    doc = staff.list_staff(account_factory, uid)[0]["documents"][0]
    assert doc["read_status"] == "sin_texto" and "No se pudo leer el texto" in doc["read_detail"]


def test_otra_empresa_no_puede_subir_ni_borrar_documentos_de_mi_personal(account_client, account_factory):
    uid, member_id = add_person(account_client, account_factory)
    upload(account_client, member_id, CV_CIVIL, "cv")
    doc_id = staff.list_staff(account_factory, uid)[0]["documents"][0]["id"]
    other = accounts.create_user(account_factory, "otra@example.test", PASSWORD, "Otra")
    pdf = company_documents.read_pdf(type("U", (), {"filename": "x.pdf", "stream": io.BytesIO(CV_OTHER)})())
    with pytest.raises(staff.StaffError):
        staff.add_member_document(account_factory, other, member_id, "cv", None, pdf)
    assert company_documents.delete_document(account_factory, other, doc_id) is False
    assert upload(account_client, member_id, CV_OTHER, "inventado").status_code == 302      # tipo inválido
    assert len(staff.list_staff(account_factory, uid)[0]["documents"]) == 1


def test_al_quitar_a_la_persona_se_borran_sus_documentos(account_client, account_factory):
    uid, member_id = add_person(account_client, account_factory)
    upload(account_client, member_id, CV_CIVIL, "cv")
    assert account_client.post(f"/perfil/personal/{member_id}/eliminar").status_code == 302
    with account_factory() as db:
        assert db.scalar(select(func.count()).select_from(company_documents.CompanyDocument)) == 0


def test_se_puede_borrar_un_documento_de_la_persona(account_client, account_factory):
    uid, member_id = add_person(account_client, account_factory)
    upload(account_client, member_id, CV_CIVIL, "cv")
    doc_id = staff.list_staff(account_factory, uid)[0]["documents"][0]["id"]
    assert account_client.post(f"/perfil/personal/documentos/{doc_id}/eliminar").status_code == 302
    assert staff.list_staff(account_factory, uid)[0]["documents"] == []


# ---------------------------------------------------------------------------
# Semáforo
# ---------------------------------------------------------------------------
def with_support(kinds, mismatch=()):
    person = member(30)
    person["evidence"] = {"kinds": sorted(kinds), "mismatch": list(mismatch), "expired": []}
    return person


def reason(result):
    return check(result, "k0")["reason"]


def test_sin_sustento_el_cargo_queda_por_sustentar():
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {"reviews": {"k0": "own"}}, TODAY,
                                  members=[with_support([])])
    assert check(result, "k0")["status"] == "pending"
    assert ("falta su sustento en Mi empresa › Personal clave: título o currículum, constancia de colegiatura y "
            "habilidad, constancias de experiencia") in reason(result)


def test_con_curriculum_y_constancia_vigente_el_cargo_queda_sustentado():
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {"reviews": {"k0": "own"}}, TODAY,
                                  members=[with_support(["cv", "colegiatura"])])
    assert "sustento" not in reason(result) and "ingeniero civil" not in reason(result).lower()


def test_un_documento_que_no_coincide_se_avisa():
    detail = "No menciona ingeniero civil; menciona ingeniero de sistemas."
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {"reviews": {"k0": "own"}}, TODAY,
                                  members=[with_support(["cv", "colegiatura"], [detail])])
    assert f"Revise los documentos de Ana (ingeniero civil): {detail}" in reason(result)


def test_entre_dos_que_cumplen_se_elige_a_quien_tiene_su_sustento():
    sin = with_support([])
    sin["experience_months"] = 60
    con = with_support(["titulo", "colegiatura", "experiencia"])
    con["id"] = 2
    found = staff.match_requirements([{"profession": "ingeniero civil", "label": "Ingeniero civil", "months": 24,
                                       "licensed": True}], [sin, con])
    assert found[0]["member"]["id"] == 2 and found[0]["missing"] == []


def test_una_constancia_vencida_se_pide_vigente():
    person = member(30)
    person["evidence"] = {"kinds": ["cv"], "mismatch": [], "expired": ["colegiatura"]}
    need = {"licensed": True, "months": 24}
    assert staff.missing_support(need, person) == ["constancia de habilidad vigente"]


def test_la_migracion_agrega_las_columnas_del_personal_a_documentos(tmp_path):
    from sqlalchemy import create_engine, inspect
    from services import db
    engine = create_engine(f"sqlite:///{tmp_path / 'cuentas.db'}")
    accounts.initialize_schema(engine)
    with engine.begin() as connection:
        for name in company_documents.STAFF_COLUMNS:
            connection.exec_driver_sql(f"ALTER TABLE documentos_empresa DROP COLUMN {name}")
    accounts.initialize_schema(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("documentos_empresa")}
    assert set(company_documents.STAFF_COLUMNS) <= columns
    engine.dispose()

def test_al_agregar_una_persona_se_abre_su_seccion_para_subir_documentos(account_client, account_factory):
    uid = signed_in(account_client, account_factory)
    html = account_client.get("/perfil/personal").get_data(as_text=True)
    assert "Primero registre sus datos" in html and 'id="profession" name="profession" type="text"' in html
    response = account_client.post("/perfil/personal", data={"name": "Juan Pérez", "profession": "Ingeniero civil",
                                                             "years": "3", "months": "0"})
    member_id = staff.list_staff(account_factory, uid)[0]["id"]
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"?nueva={member_id}#persona-{member_id}")
    html = account_client.get(f"/perfil/personal?nueva={member_id}").get_data(as_text=True)
    assert html.count('<details class="staff-upload" open>') == 1
    assert account_client.get("/perfil/personal").get_data(as_text=True).count('<details class="staff-upload" open>') == 0


def test_la_lista_de_profesiones_muestra_los_nombres_completos(account_client, account_factory):
    """En Safari la lista nativa cortaba «Ingeniero de telecomunicaciones» (24/11/2026): se usa una lista propia,
    y sin JavaScript queda la nativa."""
    signed_in(account_client, account_factory)
    html = account_client.get("/perfil/personal").get_data(as_text=True)
    assert 'list="profession-options" data-combobox' in html
    assert '<option value="Ingeniero de telecomunicaciones">' in html
    assert "js/profession-combobox.js" in html
    css = (Path(__file__).resolve().parents[1] / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".combo-option" in css and "white-space: normal" in css