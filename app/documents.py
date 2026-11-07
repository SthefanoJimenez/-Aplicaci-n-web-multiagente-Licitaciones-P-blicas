"""HU-015: pantalla y archivos disponibles exclusivamente para la propia empresa."""
import io

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import auth
from services import company_documents as documents

bp = Blueprint("company_documents", __name__)


def init_app(app):
    app.register_blueprint(bp)
    # Incluye el sobre multipart; cada PDF conserva su propio límite de 5 MB.
    app.config.setdefault("MAX_CONTENT_LENGTH", documents.MAX_FILE_BYTES + 64 * 1024)
    if app.config["MAX_CONTENT_LENGTH"] is None:
        app.config["MAX_CONTENT_LENGTH"] = documents.MAX_FILE_BYTES + 64 * 1024
    app.register_error_handler(RequestEntityTooLarge, upload_too_large)


def upload_too_large(error):
    return render_template("documents/problem.html", title="El archivo es demasiado grande",
                           message="Use un PDF de hasta 5 MB y vuelva a seleccionarlo."), 413


def unavailable():
    return render_template("documents/problem.html", title="No se pudo consultar sus documentos",
                           message="Intente de nuevo en unos minutos. Sus documentos no se han publicado."), 503


@bp.route("/documentos", methods=["GET", "POST"])
def index():
    values, errors = {}, {}
    try:
        factory = auth.store()
        if request.method == "POST":
            values, errors, metadata = documents.validate_metadata(request.form)
            if not errors:
                try:
                    pdf = documents.read_pdf(request.files.get("file"))
                    documents.add_document(factory, g.current_user["id"], metadata, pdf)
                except documents.DocumentError as error:
                    errors["file"] = str(error)
                else:
                    flash("Documento guardado. Solo su empresa puede acceder a él.", "success")
                    return redirect(url_for("company_documents.index"))
        rows = documents.list_documents(factory, g.current_user["id"])
    except SQLAlchemyError:
        return unavailable()
    return render_template("documents/index.html", documents=rows, types=documents.DOCUMENT_TYPES,
                           values=values, errors=errors, max_documents=documents.MAX_DOCUMENTS,
                           used_mb=sum(d["size_bytes"] for d in rows) / (1024 * 1024)), (400 if errors else 200)


@bp.get("/documentos/<int:document_id>/archivo")
def file(document_id):
    try:
        doc = documents.get_document(auth.store(), g.current_user["id"], document_id, include_content=True)
    except SQLAlchemyError:
        return unavailable()
    if doc is None:
        abort(404)
    response = send_file(io.BytesIO(doc["content"]), mimetype="application/pdf",
                         as_attachment=request.args.get("descargar") == "1",
                         download_name=secure_filename(doc["filename"]) or "documento.pdf", conditional=False)
    response.headers.update({"Cache-Control": "no-store", "Content-Security-Policy": "sandbox; default-src 'none'",
                             "X-Frame-Options": "SAMEORIGIN", "Cross-Origin-Resource-Policy": "same-origin"})
    return response


@bp.route("/documentos/<int:document_id>/editar", methods=["GET", "POST"])
def edit(document_id):
    errors = {}
    try:
        factory = auth.store()
        doc = documents.get_document(factory, g.current_user["id"], document_id)
        if doc is None:
            abort(404)
        values = {"document_type": doc["document_type"], "valid_from": str(doc["valid_from"] or ""),
                  "expires_on": str(doc["expires_on"] or ""), "no_expiry": doc["expires_on"] is None}
        if request.method == "POST":
            values, errors, metadata = documents.validate_metadata(request.form)
            if not errors:
                if not documents.edit_document(factory, g.current_user["id"], document_id, metadata):
                    abort(404)
                flash("Tipo y vigencia del documento actualizados.", "success")
                return redirect(url_for("company_documents.index"))
    except SQLAlchemyError:
        return unavailable()
    return render_template("documents/edit.html", document=doc, values=values, errors=errors,
                           types=documents.DOCUMENT_TYPES), (400 if errors else 200)


@bp.route("/documentos/<int:document_id>/eliminar", methods=["GET", "POST"])
def delete(document_id):
    try:
        factory = auth.store()
        doc = documents.get_document(factory, g.current_user["id"], document_id)
        if doc is None:
            abort(404)
        if request.method == "POST":
            if not documents.delete_document(factory, g.current_user["id"], document_id):
                abort(404)
            flash("Documento eliminado de su empresa.", "success")
            return redirect(url_for("company_documents.index"))
    except SQLAlchemyError:
        return unavailable()
    return render_template("documents/delete.html", document=doc)
