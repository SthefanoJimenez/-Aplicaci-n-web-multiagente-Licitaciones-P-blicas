"""Personal clave de la empresa (pedido del asesor, 19/11/2026).

Rutas bajo /perfil, que auth.PRIVATE_PREFIXES ya protege con sesión iniciada. Cada empresa solo ve y
borra su propio personal (services/staff.py busca por la empresa de la sesión).

20/11/2026: a cada persona se le suben sus fichas técnicas y sustentos (currículum, título, constancia de
habilidad, experiencia, capacitación) en /perfil/personal/<id>/documentos. Se ven y descargan con las rutas
de Documentos de mi empresa, que ya comprueban que el documento sea de la propia empresa.
"""
from datetime import date
import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

import auth
from services import company_documents, staff

bp = Blueprint("staff", __name__)


def init_app(app):
    app.register_blueprint(bp)


def unavailable():
    return render_template("documents/problem.html", title="No se pudo consultar su personal",
                           message="Intente de nuevo en unos minutos. No se ha modificado su personal."), 503


@bp.route("/perfil/personal", methods=["GET", "POST"])
def index():
    values, errors = {}, {}
    try:
        factory = auth.store()
        if request.method == "POST":
            values, errors = staff.validate(request.form)
            if not errors:
                try:
                    member_id = staff.add_member(factory, g.current_user["id"], values)
                    flash("Persona agregada. Ahora puede subir su currículum, título o certificados.", "success")
                    return redirect(url_for("staff.index", nueva=member_id, _anchor=f"persona-{member_id}"))
                except staff.StaffError as error:
                    errors["name"] = str(error)
        members = staff.list_staff(factory, g.current_user["id"])
    except SQLAlchemyError:
        return unavailable()
    new_member = request.args.get("nueva", type=int)
    return render_template("staff/index.html", members=members, values=values, errors=errors, new_member=new_member,
                           professions=staff.PROFESSIONS, max_staff=staff.MAX_STAFF,
                           months_text=staff.months_text, doc_kinds=company_documents.STAFF_DOC_KINDS), \
        (400 if errors else 200)


@bp.post("/perfil/personal/<int:member_id>/documentos")
def add_document(member_id):
    """Sube una ficha técnica o un sustento de una persona del personal clave."""
    anchor = url_for("staff.index") + f"#persona-{member_id}"
    raw = (request.form.get("expires_on") or "").strip()
    expires_on = None
    if raw:
        try:
            expires_on = date.fromisoformat(raw) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else None
        except ValueError:
            expires_on = None
        if expires_on is None:
            flash("Escriba una fecha de vigencia válida o déjela en blanco.", "error")
            return redirect(anchor)
    try:
        pdf = company_documents.read_pdf(request.files.get("file"))
        reading = staff.add_member_document(auth.store(), g.current_user["id"], member_id,
                                            request.form.get("kind", ""), expires_on, pdf)
    except (staff.StaffError, company_documents.DocumentError) as error:
        flash(str(error), "error")
        return redirect(anchor)
    except SQLAlchemyError:
        return unavailable()
    flash(f"Documento guardado. {reading['detail']}", "success")
    return redirect(anchor)


@bp.post("/perfil/personal/documentos/<int:document_id>/eliminar")
def delete_document(document_id):
    try:
        doc = company_documents.get_document(auth.store(), g.current_user["id"], document_id)
        if doc is None or not company_documents.delete_document(auth.store(), g.current_user["id"], document_id):
            abort(404)
    except SQLAlchemyError:
        return unavailable()
    flash("Documento eliminado.", "success")
    return redirect(url_for("staff.index"))


@bp.post("/perfil/personal/<int:member_id>/eliminar")
def delete(member_id):
    try:
        if not staff.delete_member(auth.store(), g.current_user["id"], member_id):
            abort(404)
    except SQLAlchemyError:
        return unavailable()
    flash("Persona quitada de su personal clave.", "success")
    return redirect(url_for("staff.index"))