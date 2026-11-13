"""HU-012: armar un consorcio con otras empresas registradas y evaluarlo frente a lo que piden las bases.

Todas las rutas están bajo /consorcio, que auth.PRIVATE_PREFIXES ya protege con sesión iniciada.
"""
import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

import auth
from services import consortia, tenders

bp = Blueprint("consortium", __name__)


def init_app(app):
    app.register_blueprint(bp)


def unavailable():
    return render_template("consortium/problem.html", title="No se pudo consultar sus consorcios",
                           message="Intente de nuevo en unos minutos. No se ha modificado ningún consorcio."), 503


def tender_code():
    """Código de la convocatoria con la que se quiere comparar (viene de su ficha)."""
    code = (request.values.get("convocatoria") or "").strip()
    return code if re.fullmatch(r"[\w-]{1,60}", code) else None


def tender_context(code):
    """(convocatoria, monto facturado que piden sus bases) o (None, None) si no se puede leer."""
    if not code:
        return None, None
    try:
        tender, reqs = tenders.tender_sheet(code)
    except tenders.TendersUnavailable:
        return None, None
    if tender is None:
        return None, None
    return tender, consortia.amount_from_requirements(reqs)


@bp.route("/consorcio", methods=["GET", "POST"])
def index():
    errors, values = {}, {}
    code = tender_code()
    try:
        factory = auth.store()
        if request.method == "POST":
            values["name"] = request.form.get("name", "")
            try:
                consortium_id = consortia.create(factory, g.current_user["id"], values["name"])
            except consortia.ConsortiumError as error:
                errors["name"] = str(error)
            else:
                flash("Consorcio creado. Ahora invite a las empresas que lo integrarán.", "success")
                return redirect(url_for("consortium.detail", consortium_id=consortium_id, convocatoria=code))
        data = consortia.overview(factory, g.current_user["id"], g.current_user["email"])
    except SQLAlchemyError:
        return unavailable()
    tender, _ = tender_context(code)
    return render_template("consortium/index.html", data=data, values=values, errors=errors,
                           code=code, tender=tender), (400 if errors else 200)


def render_detail(consortium_id, invite_email="", invite_error=None):
    factory = auth.store()
    data = consortia.detail(factory, g.current_user["id"], consortium_id)
    if data is None:
        abort(404)
    code = tender_code()
    tender, from_bases = tender_context(code)
    args = request.args.to_dict()
    if from_bases and not args.get("facturado"):
        args["facturado"] = f"{from_bases['amount']:.2f}"     # se propone el monto de las bases; se puede cambiar
    form_values, required, form_errors = consortia.parse_required(args)
    result = consortia.evaluate(data["members"], required, data["company_id"]) if not form_errors else None
    totals = consortia.combine(data["members"])["totals"]
    status = 400 if (invite_error or form_errors) else 200
    return render_template("consortium/detail.html", c=data, result=result, totals=totals, form_values=form_values,
                           form_errors=form_errors, code=code, tender=tender, from_bases=from_bases,
                           capabilities=consortia.CAPABILITIES, invite_email=invite_email,
                           invite_error=invite_error, max_partners=consortia.MAX_PARTNERS), status


@bp.get("/consorcio/<int:consortium_id>")
def detail(consortium_id):
    try:
        return render_detail(consortium_id)
    except SQLAlchemyError:
        return unavailable()


@bp.post("/consorcio/<int:consortium_id>/invitar")
def invite(consortium_id):
    email = request.form.get("email", "")
    try:
        try:
            invited = consortia.invite(auth.store(), g.current_user["id"], consortium_id, email)
        except consortia.ConsortiumError as error:
            return render_detail(consortium_id, invite_email=email.strip()[:254], invite_error=str(error))
        if not invited:
            abort(404)
    except SQLAlchemyError:
        return unavailable()
    flash("Invitación registrada. La empresa la verá en «Consorcios» al ingresar con ese correo; "
          "si aún no tiene cuenta, la verá cuando se registre con él.", "success")
    return redirect(url_for("consortium.detail", consortium_id=consortium_id))


@bp.post("/consorcio/invitaciones/<int:member_id>/<action>")
def respond(member_id, action):
    if action not in ("aceptar", "rechazar"):
        abort(404)
    try:
        consortium_id = consortia.respond(auth.store(), g.current_user["id"], g.current_user["email"],
                                          member_id, accept=action == "aceptar")
    except SQLAlchemyError:
        return unavailable()
    if consortium_id is None:
        abort(404)
    if action == "aceptar":
        flash("Su empresa ya integra el consorcio. Las demás integrantes verán los datos de su perfil.", "success")
        return redirect(url_for("consortium.detail", consortium_id=consortium_id))
    flash("Invitación rechazada.", "success")
    return redirect(url_for("consortium.index"))


@bp.post("/consorcio/<int:consortium_id>/integrantes/<int:member_id>/quitar")
def remove(consortium_id, member_id):
    try:
        outcome = consortia.remove(auth.store(), g.current_user["id"], consortium_id, member_id)
    except SQLAlchemyError:
        return unavailable()
    if outcome is None:
        abort(404)
    if outcome == "retirada":
        flash("Su empresa se retiró del consorcio.", "success")
        return redirect(url_for("consortium.index"))
    flash("Empresa o invitación quitada del consorcio.", "success")
    return redirect(url_for("consortium.detail", consortium_id=consortium_id))


@bp.route("/consorcio/<int:consortium_id>/eliminar", methods=["GET", "POST"])
def delete(consortium_id):
    try:
        factory = auth.store()
        data = consortia.detail(factory, g.current_user["id"], consortium_id)
        if data is None or data["role"] != "lider":
            abort(404)
        if request.method == "POST":
            consortia.delete_consortium(factory, g.current_user["id"], consortium_id)
            flash("Consorcio eliminado. Los perfiles de las empresas no cambian.", "success")
            return redirect(url_for("consortium.index"))
    except SQLAlchemyError:
        return unavailable()
    return render_template("consortium/delete.html", c=data)