"""HU-015: carga real de PDF, persistencia, cuotas y aislamiento entre empresas.

Usa SQLite temporal y PDF sintéticos, sin cuentas ni documentos de empresas reales.
"""
from datetime import date
import io
import subprocess

import pytest
from pypdf import PdfWriter
from sqlalchemy import func, select

from app import app
from services import accounts, company_documents as documents


def pdf_bytes(*, pages=1, encrypted=False, javascript=False, attachment=False, label="Prueba"):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": label})
    if javascript:
        writer.add_js("app.alert('test');")
    if attachment:
        writer.add_attachment("archivo.txt", b"test")
    if encrypted:
        writer.encrypt("clave de prueba")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def owner(account_client, account_factory):
    uid = accounts.create_user(account_factory, "documentos@example.test", "Una frase de prueba 2026", "Empresa sintética")
    with account_client.session_transaction() as sess:
        sess["auth_token"] = accounts.begin_session(account_factory, uid)
    return account_client


def upload(client, content=None, filename="constancia.pdf", **extra):
    payload = {"document_type": "rnp", "expires_on": "2027-12-31", **extra,
               "file": (io.BytesIO(content if content is not None else pdf_bytes()), filename)}
    return client.post("/documentos", data=payload)


def count_documents(factory):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(documents.CompanyDocument))


def test_upload_view_download_edit_delete_roundtrip(owner, account_factory):
    original = pdf_bytes(label="Constancia sintética de prueba")
    response = upload(owner, original, filename="Constancia de experiencia.pdf", document_type="experiencia")
    assert response.status_code == 302 and response.location == "/documentos"
    listing = owner.get("/documentos")
    assert listing.status_code == 200 and "Constancia de experiencia.pdf" in listing.text
    assert "Experiencia" in listing.text and "2027" in listing.text
    assert listing.headers["Cache-Control"] == "no-store"
    assert "Documentos de mi empresa" in owner.get("/perfil").text
    with account_factory() as db:
        doc = db.scalar(select(documents.CompanyDocument))
        assert doc.company_id == 1 and doc.content == original
        assert doc.size_bytes == len(original) and doc.page_count == 1
    view = owner.get("/documentos/1/archivo")
    assert view.status_code == 200 and view.data == original
    assert view.mimetype == "application/pdf" and view.headers["Content-Disposition"].startswith("inline;")
    assert view.headers["Cache-Control"] == "no-store"
    assert view.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in view.headers["Content-Security-Policy"]
    assert owner.get("/documentos/1/archivo?descargar=1").headers["Content-Disposition"].startswith("attachment;")
    assert owner.get("/documentos/1/editar").status_code == 200
    assert owner.post("/documentos/1/editar", data={"document_type": "personal", "no_expiry": "1"}).status_code == 302
    assert "Sin vencimiento declarado" in owner.get("/documentos").text
    # GET ofrece confirmación sin borrar.
    confirmation = owner.get("/documentos/1/eliminar")
    assert confirmation.status_code == 200 and "Conservar documento" in confirmation.text
    assert count_documents(account_factory) == 1
    assert owner.post("/documentos/1/eliminar").status_code == 302
    assert count_documents(account_factory) == 0
    assert owner.get("/documentos/1/archivo").status_code == 404


def test_documents_survive_new_browser_login(owner):
    upload(owner)
    owner.post("/salir")
    browser = type(owner)(app, app.response_class)
    response = browser.post("/ingresar", data={"email": "documentos@example.test", "password": "Una frase de prueba 2026"})
    assert response.status_code == 302
    assert "constancia.pdf" in browser.get("/documentos").text
    assert browser.get("/documentos/1/archivo").data == pdf_bytes()


@pytest.mark.parametrize("path", ["/documentos", "/documentos/1/archivo", "/documentos/1/editar", "/documentos/1/eliminar"])
def test_document_routes_require_login(account_client, path):
    response = account_client.get(path)
    assert response.status_code == 302 and "/ingresar?next=" in response.location


def test_other_company_cannot_list_view_edit_or_delete(owner, account_factory):
    upload(owner, filename="privado-empresa-uno.pdf")
    other_id = accounts.create_user(account_factory, "otra@example.test", "Otra frase de prueba 2026", "Otra empresa")
    other = type(owner)(app, app.response_class)
    with other.session_transaction() as sess:
        sess["auth_token"] = accounts.begin_session(account_factory, other_id)
    html = other.get("/documentos?company_id=1&user_id=1").text
    assert "privado-empresa-uno.pdf" not in html
    for path in ("/documentos/1/archivo", "/documentos/1/editar", "/documentos/1/eliminar"):
        assert other.get(path).status_code == 404
    assert other.post("/documentos/1/eliminar").status_code == 404
    assert other.post("/documentos/1/editar", data={"document_type": "otro", "no_expiry": "1"}).status_code == 404
    assert count_documents(account_factory) == 1
    # El ID enviado no decide la empresa y un mismo archivo puede pertenecer a empresas distintas.
    assert upload(other, company_id="1").status_code == 302
    with account_factory() as db:
        assert db.get(documents.CompanyDocument, 2).company_id == 2
    assert documents.evaluation_inventory(account_factory, other_id)["rnp"]["available"] == [2]


@pytest.mark.parametrize("path", ["/documentos", "/documentos/1/editar", "/documentos/1/eliminar"])
def test_mutations_require_csrf(owner, account_factory, path):
    upload(owner)
    assert owner.open(path, method="POST", data={}).status_code == 400
    assert count_documents(account_factory) == 1


@pytest.mark.parametrize("filename,content", [("archivo.txt", b"not pdf"), ("archivo.pdf", b"%PDF-1.7 fake %%EOF"),
                                             ("archivo.pdf", b""), ("archivo.pdf.exe", b"not pdf"),
                                             ("a:archivo.pdf", b"not pdf"), ("x" * 201 + ".pdf", b"not pdf")])
def test_invalid_files_rejected_without_save(owner, account_factory, filename, content):
    response = upload(owner, content, filename)
    assert response.status_code == 400 and 'role="alert"' in response.text
    assert count_documents(account_factory) == 0


@pytest.mark.parametrize("settings", [{"encrypted": True}, {"javascript": True}, {"attachment": True}, {"pages": 201}, {"pages": 0}])
def test_unsupported_pdf_is_rejected(owner, account_factory, settings):
    assert upload(owner, pdf_bytes(**settings)).status_code == 400
    assert count_documents(account_factory) == 0


def test_missing_file_oversized_file_and_request(owner, account_factory):
    assert owner.post("/documentos", data={"document_type": "rnp", "no_expiry": "1"}).status_code == 400
    oversized = b"%PDF-1.7\n" + b"x" * documents.MAX_FILE_BYTES + b"\n%%EOF"
    assert upload(owner, oversized).status_code == 400
    assert upload(owner, b"x" * (documents.MAX_FILE_BYTES + 100000)).status_code == 413
    assert count_documents(account_factory) == 0


@pytest.mark.parametrize("fields", [{"document_type": "falso"}, {"expires_on": ""}, {"expires_on": "2026-02-30"},
                                   {"valid_from": "2026-99-01"}, {"valid_from": "2028-01-01"},
                                   {"expires_on": "2027-12-31", "no_expiry": "1"}])
def test_invalid_metadata_never_saves_pdf(owner, account_factory, fields):
    assert upload(owner, **fields).status_code == 400
    assert count_documents(account_factory) == 0


def test_expiry_boundaries_and_inventory_for_evaluation(owner, account_factory, monkeypatch):
    monkeypatch.setattr(documents, "today", lambda: date(2026, 11, 7))
    for i, fields in enumerate([{"expires_on": "2026-11-06"}, {"expires_on": "2026-11-07"},
                                {"valid_from": "2026-11-08", "expires_on": "2027-01-01"},
                                {"expires_on": "", "no_expiry": "1"}]):
        assert upload(owner, pdf_bytes(label=str(i)), **fields).status_code == 302
    inventory = documents.evaluation_inventory(account_factory, 1)
    assert set(inventory["rnp"]["available"]) == {2, 4}
    assert inventory["rnp"]["expired"] == [1] and inventory["rnp"]["future"] == [3]
    assert inventory["personal"]["available"] == []
    assert set(documents.evaluation_inventory(account_factory, 1, date(2027, 2, 1))["rnp"]["expired"]) == {1, 2, 3}
    html = owner.get("/documentos").text
    for expected in ("Vencido", "Aún no vigente", "Vigente según fechas declaradas", "Sin vencimiento declarado"):
        assert expected in html
    owner.post("/documentos/2/eliminar")
    assert documents.evaluation_inventory(account_factory, 1)["rnp"]["available"] == [4]


def test_duplicate_quota_and_metadata_edit_are_atomic(owner, account_factory, monkeypatch):
    assert upload(owner).status_code == 302
    assert upload(owner).status_code == 400
    monkeypatch.setattr(documents, "MAX_DOCUMENTS", 1)
    assert upload(owner, pdf_bytes(label="distinto")).status_code == 400
    monkeypatch.setattr(documents, "MAX_DOCUMENTS", 30)
    monkeypatch.setattr(documents, "MAX_COMPANY_BYTES", 1)
    assert upload(owner, pdf_bytes(label="distinto")).status_code == 400
    assert count_documents(account_factory) == 1
    invalid = owner.post("/documentos/1/editar", data={"document_type": "otro", "expires_on": ""})
    assert invalid.status_code == 400
    with account_factory() as db:
        assert db.get(documents.CompanyDocument, 1).document_type == "rnp"


def test_pdf_timeout_and_database_failure_are_safe(owner, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("parser", 8)
    monkeypatch.setattr(documents.subprocess, "run", timeout)
    assert "demasiado complejo" in upload(owner).text
    import auth
    from sqlalchemy.exc import SQLAlchemyError
    def fail():
        raise SQLAlchemyError("PRIVATE_CONNECTION_SECRET")
    monkeypatch.setattr(auth, "store", fail)
    response = owner.get("/documentos")
    assert response.status_code == 503 and "PRIVATE_CONNECTION_SECRET" not in response.text


def test_original_filename_is_escaped_and_never_used_as_storage_path(owner, account_factory):
    assert upload(owner, filename="../../constancia.pdf").status_code == 302
    assert documents.get_document(account_factory, 1, 1)["filename"] == "constancia.pdf"
    with owner.session_transaction() as sess:
        assert "constancia.pdf" not in str(dict(sess))
