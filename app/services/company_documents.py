"""HU-015: PDF privados y vigencia declarada. No envía archivos a modelos ni trazas."""
from datetime import date, datetime
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from services.accounts import AccountBase, Company, now

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_COMPANY_BYTES = 50 * 1024 * 1024
MAX_DOCUMENTS = 30
MAX_PAGES = 200
DOCUMENT_TYPES = {"rnp": "RNP", "experiencia": "Experiencia", "personal": "Personal", "otro": "Otro"}


class DocumentError(ValueError):
    """Mensaje apto para mostrar sin exponer contenido o datos de conexión."""


class CompanyDocument(AccountBase):
    __tablename__ = "documentos_empresa"
    __table_args__ = (UniqueConstraint("company_id", "sha256", name="uq_documento_empresa_sha256"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    filename: Mapped[str] = mapped_column(String(200))
    document_type: Mapped[str] = mapped_column(String(20))
    valid_from: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def today():
    return datetime.now(ZoneInfo("America/Lima")).date()


def validate_metadata(form):
    values = {key: form.get(key, "").strip() for key in ("document_type", "valid_from", "expires_on")}
    values["no_expiry"] = form.get("no_expiry") == "1"
    errors = {}
    if values["document_type"] not in DOCUMENT_TYPES:
        errors["document_type"] = "Seleccione el tipo de documento."
    dates = {}
    for key in ("valid_from", "expires_on"):
        raw = values[key]
        try:
            dates[key] = date.fromisoformat(raw) if raw and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else None
            if raw and dates[key] is None:
                raise ValueError
        except ValueError:
            dates[key] = None
            errors[key] = "Escriba una fecha válida."
    if values["no_expiry"] and values["expires_on"]:
        errors["expires_on"] = "Quite la fecha o desmarque «Sin vencimiento declarado»."
    elif not values["no_expiry"] and not values["expires_on"]:
        errors["expires_on"] = "Indique el vencimiento o marque «Sin vencimiento declarado»."
    if dates["valid_from"] and dates["expires_on"] and dates["expires_on"] < dates["valid_from"]:
        errors["expires_on"] = "El vencimiento no puede ser anterior al inicio de vigencia."
    return values, errors, {"document_type": values["document_type"], **dates}


def read_pdf(upload):
    """Límite antes de analizar; valida en un proceso aislado con tiempo máximo."""
    if upload is None or not upload.filename:
        raise DocumentError("Seleccione un archivo PDF.")
    original = unicodedata.normalize("NFC", upload.filename)
    filename = original.replace("\\", "/").rsplit("/", 1)[-1]
    if (not filename.lower().endswith(".pdf") or len(filename) > 200 or
            any(ord(c) < 32 or c in ':<>|?*' for c in filename)):
        raise DocumentError("Use un archivo con extensión .pdf y un nombre de hasta 200 caracteres.")
    content = upload.stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise DocumentError("El PDF supera el máximo de 5 MB.")
    if not content or not re.match(rb"%PDF-[12]\.\d", content) or b"%%EOF" not in content[-1024:]:
        raise DocumentError("El archivo no contiene un PDF válido y completo.")
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("pdf_validation.py"))],
                                input=content, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=8, check=False)
    except subprocess.TimeoutExpired:
        raise DocumentError("El PDF es demasiado complejo para revisarlo. Exporte una copia más sencilla.") from None
    except OSError:
        raise DocumentError("No se pudo iniciar la revisión del PDF. Intente de nuevo en unos minutos.") from None
    if result.returncode != 0:
        raise DocumentError("No se pudo revisar el PDF. Use un PDF legible, sin contraseña, archivos adjuntos ni acciones automáticas (máximo 200 páginas).")
    try:
        pages = int(result.stdout)
        if not 1 <= pages <= MAX_PAGES:
            raise ValueError
    except ValueError:
        raise DocumentError("No se pudo revisar el PDF. Intente con otra copia.") from None
    return {"filename": filename, "content": content, "size_bytes": len(content),
            "page_count": pages, "sha256": hashlib.sha256(content).hexdigest()}


def _owned_query(user_id):
    return select(CompanyDocument).join(Company).where(Company.user_id == user_id)


def document_status(document, on_date=None):
    day = on_date or today()
    if document.valid_from and document.valid_from > day:
        return "future", "Aún no vigente"
    if document.expires_on and document.expires_on < day:
        return "expired", "Vencido"
    return ("current", "Vigente según fechas declaradas") if document.expires_on else ("current", "Sin vencimiento declarado")


def list_documents(factory, user_id, on_date=None):
    with factory() as db:
        rows = db.scalars(_owned_query(user_id).order_by(CompanyDocument.uploaded_at.desc(), CompanyDocument.id.desc())).all()
        result = []
        for doc in rows:
            state, label = document_status(doc, on_date)
            result.append({key: getattr(doc, key) for key in ("id", "filename", "document_type", "valid_from", "expires_on", "size_bytes", "page_count", "uploaded_at")} |
                          {"state": state, "state_label": label, "type_label": DOCUMENT_TYPES[doc.document_type]})
        return result


def add_document(factory, user_id, metadata, pdf):
    with factory.begin() as db:
        # Escritura inocua: serializa cuotas incluso en SQLite; en PostgreSQL bloquea solo la empresa.
        db.execute(update(Company).where(Company.user_id == user_id).values(name=Company.name))
        company = db.scalar(select(Company).where(Company.user_id == user_id))
        count, used = db.execute(select(func.count(), func.coalesce(func.sum(CompanyDocument.size_bytes), 0))
                                .where(CompanyDocument.company_id == company.id)).one()
        if count >= MAX_DOCUMENTS or used + pdf["size_bytes"] > MAX_COMPANY_BYTES:
            raise DocumentError("Su empresa alcanzó el límite de 30 documentos o 50 MB. Borre uno que ya no necesite.")
        if db.scalar(select(CompanyDocument.id).where(CompanyDocument.company_id == company.id, CompanyDocument.sha256 == pdf["sha256"])):
            raise DocumentError("Este PDF ya está guardado en su empresa. Puede actualizar su tipo y vigencia desde el listado.")
        doc = CompanyDocument(company_id=company.id, **metadata, **pdf)
        db.add(doc)
        db.flush()
        return doc.id


def get_document(factory, user_id, document_id, include_content=False):
    with factory() as db:
        doc = db.scalar(_owned_query(user_id).where(CompanyDocument.id == document_id))
        if doc is None:
            return None
        result = {key: getattr(doc, key) for key in ("id", "filename", "document_type", "valid_from", "expires_on", "size_bytes", "page_count", "uploaded_at")}
        if include_content:
            result["content"] = doc.content
        return result


def edit_document(factory, user_id, document_id, metadata):
    with factory.begin() as db:
        doc = db.scalar(_owned_query(user_id).where(CompanyDocument.id == document_id))
        if doc is None:
            return False
        for key in ("document_type", "valid_from", "expires_on"):
            setattr(doc, key, metadata[key])
        return True


def delete_document(factory, user_id, document_id):
    with factory.begin() as db:
        doc = db.scalar(_owned_query(user_id).where(CompanyDocument.id == document_id))
        if doc is None:
            return False
        db.delete(doc)
        return True


def evaluation_inventory(factory, user_id, on_date=None):
    """Contrato para HU-010: disponibilidad declarada por tipo, sin contenido de PDF.

    Una carga no prueba autenticidad, inscripción vigente ni cumplimiento de las bases.
    La fecha se puede pasar desde la evaluación (por ejemplo, cierre de la convocatoria).
    """
    docs = list_documents(factory, user_id, on_date)
    return {kind: {"available": [d["id"] for d in docs if d["document_type"] == kind and d["state"] == "current"],
                   "expired": [d["id"] for d in docs if d["document_type"] == kind and d["state"] == "expired"],
                   "future": [d["id"] for d in docs if d["document_type"] == kind and d["state"] == "future"]}
            for kind in DOCUMENT_TYPES}
