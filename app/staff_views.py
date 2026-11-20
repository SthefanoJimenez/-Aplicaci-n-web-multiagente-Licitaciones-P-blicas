"""Personal clave de la empresa (pedido del asesor, 19/11/2026).

Rutas bajo /perfil, que auth.PRIVATE_PREFIXES ya protege con sesión iniciada. Cada empresa solo ve y
borra su propio personal (services/staff.py busca por la empresa de la sesión).
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

import auth
from services import staff

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
                    staff.add_member(factory, g.current_user["id"], values)
                    flash("Persona agregada a su personal clave.", "success")
                    return redirect(url_for("staff.index"))
                except staff.StaffError as error:
                    errors["name"] = str(error)
        members = staff.list_staff(factory, g.current_user["id"])
    except SQLAlchemyError:
        return unavailable()
    return render_template("staff/index.html", members=members, values=values, errors=errors,
                           professions=staff.PROFESSIONS, max_staff=staff.MAX_STAFF,
                           months_text=staff.months_text), (400 if errors else 200)


@bp.post("/perfil/personal/<int:member_id>/eliminar")
def delete(member_id):
    try:
        if not staff.delete_member(auth.store(), g.current_user["id"], member_id):
            abort(404)
    except SQLAlchemyError:
        return unavailable()
    flash("Persona quitada de su personal clave.", "success")
    return redirect(url_for("staff.index"))