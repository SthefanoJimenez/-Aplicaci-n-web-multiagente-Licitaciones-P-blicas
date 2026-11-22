"""HU-008: registro, sesiones y perfil. Los datos privados permanecen en la base,
fuera de la cookie de Flask, del modelo de lenguaje y de las trazas de LangSmith.
"""
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
import re
import secrets
from urllib.parse import unquote, urlsplit

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.exc import SQLAlchemyError

import config
from services import accounts, db, password_recovery, profile_fields, ruc_lookup


bp = Blueprint("auth", __name__)
# Registro con dirección ASCII habitual (dot-atom) y dominio público de varias
# etiquetas. Una sola regla se comparte con el formulario para evitar diferencias.
# Sin consultas DNS: el formato no acredita que exista el buzón.
EMAIL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
EMAIL_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
EMAIL_TLD = r"(?:[A-Za-z]{2,63}|[xX][nN]--[A-Za-z0-9](?:[A-Za-z0-9-]{0,57}[A-Za-z0-9]))"
EMAIL_PATTERN = rf"(?=.{{1,64}}@){EMAIL_ATOM}(?:\.{EMAIL_ATOM})*@(?:{EMAIL_LABEL}\.)+{EMAIL_TLD}"


def password_errors(password, confirmation):
    """La misma política en registro, cambio y recuperación; no altera cuentas previas."""
    errors = {}
    if (not 4 <= len(password) <= 128 or not all(re.search(p, password) for p in
                                               (r"[A-Z]", r"[a-z]", r"[0-9]", r"[^\w\s]|_"))):
        errors["password"] = "Use de 4 a 128 caracteres, con mayúscula, minúscula, número y símbolo."
    if password != confirmation:
        errors["password_confirm"] = "Las contraseñas no coinciden."
    return errors

PRIVATE_ENDPOINTS = {"describe", "candidates", "back_to_candidates", "confirm", "market",
                     "cost", "verdict", "verdict_without_cost", "auth.profile", "evaluate_company_product"}
PRIVATE_PREFIXES = ("/perfil", "/documentos", "/consorcio", "/asistente", "/elegibilidad")
PASSWORD_ENDPOINTS = {"auth.forgot_password", "auth.reset_password", "auth.change_password"}


def password_management_enabled():
    return current_app.config.get("PASSWORD_MANAGEMENT_ENABLED", config.PASSWORD_MANAGEMENT_ENABLED) is True


def _ruc_lookup_token():
    return current_app.config.get("MIAPI_RUC_TOKEN", config.MIAPI_RUC_TOKEN)


def _ruc_lookup_enabled():
    return bool(_ruc_lookup_token() or current_app.config.get("RUC_LOOKUP_CLIENT"))


def store():
    """Motor compartido por proceso. En local usa un SQLite persistente si no hay Neon."""
    factory = current_app.config.get("ACCOUNTS_SESSION_FACTORY")
    if factory:
        return factory
    factory = current_app.extensions.get("accounts_factory")
    if factory is None:
        url = current_app.config.get("ACCOUNTS_DATABASE_URL") or config.ACCOUNTS_DATABASE_URL or config.DATABASE_URL
        if not url:
            if os.environ.get("RENDER"):
                raise SQLAlchemyError("Base de cuentas sin configurar")
            Path(current_app.instance_path).mkdir(parents=True, exist_ok=True)
            url = "sqlite:///" + str(Path(current_app.instance_path) / "cuentas.sqlite3")
        engine = db.make_engine(url)
        accounts.initialize_schema(engine, password_management=password_management_enabled(),
                                   ruc_lookup_enabled=_ruc_lookup_enabled())
        factory = db.make_session_factory(engine)
        current_app.extensions["accounts_factory"] = factory
    return factory


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def safe_next(value):
    """Solo rutas de esta aplicación, incluso si el parámetro viene manipulado."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    decoded = unquote(value)
    if decoded.startswith("//") or "\\" in decoded:
        return None
    if any(ord(c) < 32 for c in value):
        return None
    parts = urlsplit(value)
    return value if not parts.scheme and not parts.netloc else None


def _service_unavailable():
    return render_template("auth/unavailable.html"), 503


def init_app(app):
    # La clave conocida del ejemplo no puede firmar cuentas reales. En local se genera
    # una clave persistente; Render ya genera SECRET_KEY en render.yaml.
    if not app.secret_key or app.secret_key == "solo-para-desarrollo-local":
        if os.environ.get("RENDER"):
            raise RuntimeError("Configure SECRET_KEY en Render antes de habilitar cuentas.")
        folder = Path(app.instance_path)
        folder.mkdir(parents=True, exist_ok=True)
        key_file = folder / "session.key"
        try:
            with key_file.open("x") as out:
                os.chmod(key_file, 0o600)
                out.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.secret_key = key_file.read_text().strip()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")))
    app.register_blueprint(bp)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def require_account():
        g.current_user = None
        if request.endpoint == "static":
            return None
        if request.endpoint in PASSWORD_ENDPOINTS and not password_management_enabled():
            abort(404)
        unavailable = False
        token = session.get("auth_token")
        if token:
            try:
                g.current_user = accounts.current_account(store(), token)
                if not g.current_user:
                    session.clear()
            except SQLAlchemyError:
                unavailable = True
        private = request.endpoint in PRIVATE_ENDPOINTS or any(
            request.path == p or request.path.startswith(p + "/") for p in PRIVATE_PREFIXES)
        if private and not g.current_user:
            if unavailable:
                return _service_unavailable()
            # No se reenvía una petición POST con datos privados a la pantalla de ingreso.
            target = request.full_path.rstrip("?") if request.method == "GET" else url_for("describe")
            return redirect(url_for("auth.login", next=target))
        # Solo las rutas existentes: un 404 no debe convertirse en un formulario.
        if request.endpoint and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                return render_template("auth/form_expired.html"), 400

    @app.context_processor
    def account_context():
        enabled = password_management_enabled()
        return {"current_user": g.get("current_user"), "password_management_enabled": enabled,
                "password_recovery_available": bool(enabled and request.endpoint == "auth.login"
                                                     and password_recovery.settings(current_app))}

    @app.after_request
    def private_headers(response):
        if g.get("current_user") or (request.endpoint or "").startswith("auth."):
            response.headers["Cache-Control"] = "no-store"
        if request.endpoint == "auth.reset_password":
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response


def _sign_in(user_id):
    factory = store()
    accounts.end_session(factory, session.get("auth_token"))
    token = accounts.begin_session(factory, user_id)
    session.clear()  # Tampoco se comparte el flujo de evaluación de la cuenta anterior.
    session["auth_token"] = token


@bp.route("/registro", methods=["GET", "POST"])
def register():
    if g.current_user:
        return redirect(url_for("auth.profile"))
    errors, values = {}, {}
    target = safe_next(request.values.get("next"))
    if request.method == "POST":
        values = {"email": request.form.get("email", "").strip().lower(),
                  "company_name": request.form.get("company_name", "").strip()}
        password = request.form.get("password", "")
        if len(values["email"]) > 254 or not re.fullmatch(EMAIL_PATTERN, values["email"]):
            errors["email"] = "Escriba un correo válido, por ejemplo: empresa@ejemplo.com. Revise los puntos y la terminación del dominio."
        if not 2 <= len(values["company_name"]) <= 200:
            errors["company_name"] = "Escriba el nombre de su empresa (de 2 a 200 caracteres)."
        errors.update(password_errors(password, request.form.get("password_confirm")))
        if not errors:
            try:
                factory = store()
                user_id = accounts.create_user(factory, values["email"], password, values["company_name"])
                _sign_in(user_id)
                flash("Su cuenta está lista. Complete el perfil de su empresa.", "success")
                return redirect(url_for("auth.profile", **({"next": target} if target else {})))
            except accounts.EmailTaken as error:
                errors["email"] = str(error)
            except SQLAlchemyError:
                return _service_unavailable()
    return render_template("auth/register.html", errors=errors, values=values, next_url=target,
                           email_pattern=EMAIL_PATTERN), (400 if errors else 200)


@bp.route("/ingresar", methods=["GET", "POST"])
def login():
    target = safe_next(request.values.get("next"))
    if g.current_user:
        return redirect(target or url_for("auth.profile"))
    errors, email = {}, ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        password = request.form.get("password", "")
        try:
            factory = store()
            fingerprint = accounts.digest((request.remote_addr or "") + "|" + email)
            if not accounts.record_attempt(factory, fingerprint):
                errors["login"] = "Espere 15 minutos antes de volver a intentar el ingreso."
                return render_template("auth/login.html", errors=errors, email=email, next_url=target), 429
            user_id = accounts.authenticate(factory, email, password) if len(password) <= 128 else None
            if user_id:
                _sign_in(user_id)
                return redirect(target or url_for("auth.profile"))
            errors["login"] = "El correo o la contraseña no son correctos."
        except SQLAlchemyError:
            return _service_unavailable()
    return render_template("auth/login.html", errors=errors, email=email, next_url=target), (400 if errors else 200)


@bp.post("/salir")
def logout():
    try:
        accounts.end_session(store(), session.get("auth_token"))
    except SQLAlchemyError:
        return _service_unavailable()
    session.clear()
    return redirect(url_for("home"))


def _notify_password_changed(factory, user_id):
    configured = password_recovery.settings(current_app)
    if configured:
        try:
            with factory() as connection:
                user = connection.get(accounts.User, user_id)
                if user:
                    password_recovery.notify_changed(configured, user.email)
        except SQLAlchemyError:
            current_app.logger.error("No se pudo preparar la notificación de cambio.")


@bp.route("/recuperar-contrasena", methods=["GET", "POST"])
def forgot_password():
    configured = password_recovery.settings(current_app)
    if not configured:
        return render_template("auth/forgot_password.html", unavailable=True, sent=False), 503
    sent = False
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        try:
            factory = store()
            allowed_ip = accounts.recovery_attempt(factory, accounts.digest(
                "recovery-ip|" + (request.remote_addr or "")), 10)
            allowed_email = accounts.recovery_attempt(factory, accounts.digest("recovery-email|" + email), 3)
            if allowed_ip and allowed_email and re.fullmatch(EMAIL_PATTERN, email):
                password_recovery.submit(factory, configured, email)
            sent = True
        except SQLAlchemyError:
            return _service_unavailable()
    return render_template("auth/forgot_password.html", unavailable=False, sent=sent)


@bp.route("/restablecer-contrasena", methods=["GET", "POST"])
def reset_password():
    errors = {}
    # El secreto viaja en el fragmento del correo y luego en el cuerpo POST;
    # no se incluye en la URL enviada al servidor ni en el registro de accesos.
    token = request.form.get("reset_token", "")[:129] if request.method == "POST" else ""
    if request.method == "POST":
        password = request.form.get("password", "")
        errors.update(password_errors(password, request.form.get("password_confirm")))
        if not errors:
            try:
                factory = store()
                if not accounts.record_attempt(factory, accounts.digest(
                        "reset|" + (request.remote_addr or ""))):
                    errors["reset"] = "Espere 15 minutos antes de volver a intentar."
                    return render_template("auth/reset_password.html", errors=errors, token=token), 429
                with factory() as connection:
                    row = connection.get(accounts.PasswordReset, accounts.digest(token))
                    user_id = row.user_id if row else None
                if accounts.reset_password(factory, token, password):
                    session.clear()
                    _notify_password_changed(factory, user_id)
                    flash("Contraseña actualizada. Inicie sesión con su nueva contraseña.", "success")
                    return redirect(url_for("auth.login"))
                errors["reset"] = "El enlace no es válido o ya venció. Solicite uno nuevo."
            except SQLAlchemyError:
                return _service_unavailable()
    return render_template("auth/reset_password.html", errors=errors, token=token), (400 if errors else 200)


@bp.route("/perfil/contrasena", methods=["GET", "POST"])
def change_password():
    errors = {}
    if request.method == "POST":
        password = request.form.get("password", "")
        previous = request.form.get("current_password", "")
        errors.update(password_errors(password, request.form.get("password_confirm")))
        if len(previous) > 128:
            errors["current_password"] = "La contraseña actual no es correcta."
        if not errors:
            try:
                factory = store()
                if not accounts.record_attempt(factory, accounts.digest("change|" + str(g.current_user["id"]))):
                    errors["current_password"] = "Espere 15 minutos antes de volver a intentar."
                    return render_template("auth/change_password.html", errors=errors), 429
                if accounts.change_password(factory, g.current_user["id"], previous, password):
                    session.clear()
                    _notify_password_changed(factory, g.current_user["id"])
                    flash("Contraseña actualizada. Se cerraron sus sesiones. Ingrese de nuevo.", "success")
                    return redirect(url_for("auth.login"))
                errors["current_password"] = "La contraseña actual no es correcta."
            except SQLAlchemyError:
                return _service_unavailable()
    return render_template("auth/change_password.html", errors=errors), (400 if errors else 200)


def validate_profile(form):
    values, errors = {}, {}
    values["ruc"] = form.get("ruc", "").strip()
    if values["ruc"] and not accounts.valid_ruc(values["ruc"]):
        errors["ruc"] = "Escriba los 11 dígitos del RUC, sin espacios ni guiones."
    for key, maximum in (("company_name", 200), ("business_activity", 300), ("rubros", 500), ("specialized_staff", 3000)):
        value = form.get(key, "").strip()
        if not value or len(value) > maximum:
            errors[key] = f"Complete este campo (máximo {maximum} caracteres)."
        values[key] = value
    if message := profile_fields.rubros_error(values["rubros"]):
        errors["rubros"] = message
    if len(values["company_name"]) < 2:
        errors["company_name"] = "Escriba la razón social (de 2 a 200 caracteres)."
    for key in ("monthly_sales", "similar_contracts_amount"):
        raw = form.get(key, "").strip()
        try:
            if not re.fullmatch(r"[0-9]{1,14}([.,][0-9]{1,2})?", raw):
                raise InvalidOperation
            values[key] = Decimal(raw.replace(",", "."))
        except InvalidOperation:
            values[key] = raw
            errors[key] = "Escriba un monto mayor o igual a cero, sin separadores de miles y con hasta dos decimales."
    # Opcionales (19/11/2026): quién postula, qué ofrece y en qué registros del RNP está inscrita.
    supplier_type = form.get("supplier_type", "").strip()
    if supplier_type and supplier_type not in accounts.SUPPLIER_TYPES:
        errors["supplier_type"] = "Elija si postula como empresa o como persona natural."
    values["supplier_type"] = supplier_type if supplier_type in accounts.SUPPLIER_TYPES else ""
    # Como en main, los valores ajenos al catálogo se descartan sin perder las casillas válidas.
    for key, allowed in (
        ("categories", accounts.CATEGORY_LABELS),
        ("rnp_registers", accounts.RNP_REGISTERS),
    ):
        selected = set(form.getlist(key))
        values[key] = [option for option in allowed if option in selected]
    values["is_mype"] = form.get("is_mype") == "1"
    years = form.get("experience_years", "").strip()
    if message := profile_fields.experience_error(years):
        values["experience_years"] = years
        errors["experience_years"] = message
    else:
        values["experience_years"] = int(years)
    return values, errors


def _ruc_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="licitafacil-ruc-lookup")


def _ruc_proof(details):
    return _ruc_serializer().dumps({"login": accounts.digest(session["auth_token"]), "result": asdict(details)})


def _read_ruc_proof(proof, ruc):
    if not proof or len(proof) > 4096:
        return None
    try:
        data = _ruc_serializer().loads(proof, max_age=30 * 60)
        if data["login"] == accounts.digest(session["auth_token"]) and data["result"]["ruc"] == ruc:
            return ruc_lookup.RucDetails(**data["result"])
    except (BadData, KeyError, TypeError, ValueError):
        pass
    return None


@bp.post("/perfil/ruc/consultar")
def consult_ruc():
    if not _ruc_lookup_enabled():
        abort(404)
    ruc = request.form.get("ruc", "").strip()
    if not accounts.valid_ruc(ruc):
        return jsonify(message="Escriba los 11 dígitos del RUC."), 400
    try:
        daily_limit = int(current_app.config.get("MIAPI_RUC_DAILY_LIMIT", config.MIAPI_RUC_DAILY_LIMIT))
        if daily_limit < 1:
            raise ValueError
        if not accounts.reserve_ruc_lookup(store(), g.current_user["id"], daily_limit):
            return jsonify(message="Se alcanzó el límite de consultas de hoy. Puede completar la razón social a mano."), 429
        client = current_app.config.get("RUC_LOOKUP_CLIENT")
        detail = client(ruc) if client else ruc_lookup.lookup(_ruc_lookup_token(), ruc)
    except ruc_lookup.LookupError as error:
        return jsonify(message=str(error)), error.status
    except (SQLAlchemyError, ValueError):
        return jsonify(message="La consulta de RUC no está disponible en este momento."), 503
    return jsonify(**asdict(detail), proof=_ruc_proof(detail))


@bp.route("/perfil", methods=["GET", "POST"])
def profile():
    errors = {}
    target = safe_next(request.values.get("next"))
    enabled = _ruc_lookup_enabled()
    proof = ""
    detail = None
    try:
        factory, user_id = store(), g.current_user["id"]
        saved = accounts.get_saved_ruc_details(factory, user_id) if enabled else None
        if request.method == "POST":
            # Mutable para la razón social consultada; conserva todos los valores de las casillas.
            form = request.form.copy()
            ruc = form.get("ruc", "").strip()
            received_proof = form.get("ruc_lookup_proof", "") if enabled else ""
            detail = _read_ruc_proof(received_proof, ruc)
            if detail:
                proof = received_proof
            elif saved and saved.ruc == ruc:
                detail = saved
            if detail:
                form["company_name"] = detail.company_name
            values, errors = validate_profile(form)
            if received_proof and not proof and not detail:
                errors["ruc"] = "Vuelva a consultar el RUC o complete y guarde la razón social a mano."
            if not errors:
                try:
                    accounts.save_profile(factory, user_id, values, ruc_lookup_enabled=enabled, ruc_details=detail)
                    flash("Perfil guardado. Puede actualizarlo cuando lo necesite.", "success")
                    return redirect(target or url_for("auth.profile"))
                except accounts.RucTaken as error:
                    errors["ruc"] = str(error)
        else:
            values = accounts.get_profile(factory, user_id)
            if saved and saved.ruc == values["ruc"] and saved.company_name == values["company_name"]:
                detail = saved
    except SQLAlchemyError:
        return _service_unavailable()
    return render_template(
        "auth/profile.html", errors=errors, values=values, next_url=target, ruc_detail=detail, ruc_proof=proof,
        ruc_endpoint=url_for("auth.consult_ruc") if enabled else "",
        ruc_source=current_app.config.get("RUC_LOOKUP_SOURCE", "MiAPI Cloud"),
        supplier_types=accounts.SUPPLIER_TYPES, category_labels=accounts.CATEGORY_LABELS,
        rnp_registers=accounts.RNP_REGISTERS,
    ), (400 if errors else 200)
