"""HU-008: registro, sesiones y perfil. Los datos privados permanecen en la base,
fuera de la cookie de Flask, del modelo de lenguaje y de las trazas de LangSmith.
"""
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
import re
import secrets
from urllib.parse import unquote, urlsplit

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import SQLAlchemyError

import config
from services import accounts, db


bp = Blueprint("auth", __name__)
PRIVATE_ENDPOINTS = {"describe", "candidates", "back_to_candidates", "confirm", "market",
                     "cost", "verdict", "verdict_without_cost", "auth.profile"}
PRIVATE_PREFIXES = ("/perfil", "/documentos", "/consorcio", "/asistente")


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
        accounts.AccountBase.metadata.create_all(engine)
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
        return {"current_user": g.get("current_user")}

    @app.after_request
    def private_headers(response):
        if g.get("current_user") or request.endpoint in {"auth.login", "auth.register", "auth.profile"}:
            response.headers["Cache-Control"] = "no-store"
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
        if len(values["email"]) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["email"]):
            errors["email"] = "Escriba un correo válido."
        if not 2 <= len(values["company_name"]) <= 200:
            errors["company_name"] = "Escriba el nombre de su empresa (de 2 a 200 caracteres)."
        if (not 4 <= len(password) <= 128 or
                not all(re.search(pattern, password) for pattern in
                        (r"[A-Z]", r"[a-z]", r"[0-9]", r"[^\w\s]|_"))):
            errors["password"] = "Use de 4 a 128 caracteres, con mayúscula, minúscula, número y símbolo."
        if password != request.form.get("password_confirm"):
            errors["password_confirm"] = "Las contraseñas no coinciden."
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
    return render_template("auth/register.html", errors=errors, values=values, next_url=target), (400 if errors else 200)


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


def validate_profile(form):
    values, errors = {}, {}
    for key, maximum in (("company_name", 200), ("business_activity", 300), ("rubros", 500), ("specialized_staff", 3000)):
        value = form.get(key, "").strip()
        if not value or len(value) > maximum:
            errors[key] = f"Complete este campo (máximo {maximum} caracteres)."
        values[key] = value
    for key in ("monthly_sales", "similar_contracts_amount"):
        raw = form.get(key, "").strip()
        try:
            if not re.fullmatch(r"\d{1,14}([.,]\d{1,2})?", raw):
                raise InvalidOperation
            values[key] = Decimal(raw.replace(",", "."))
        except InvalidOperation:
            values[key] = raw
            errors[key] = "Escriba un monto mayor o igual a cero, sin separadores de miles y con hasta dos decimales."
    years = form.get("experience_years", "").strip()
    if not re.fullmatch(r"\d{1,3}", years) or int(years) > 200:
        values["experience_years"] = years
        errors["experience_years"] = "Escriba los años completos de experiencia, de 0 a 200."
    else:
        values["experience_years"] = int(years)
    return values, errors


@bp.route("/perfil", methods=["GET", "POST"])
def profile():
    errors = {}
    target = safe_next(request.values.get("next"))
    try:
        if request.method == "POST":
            values, errors = validate_profile(request.form)
            if not errors:
                accounts.save_profile(store(), g.current_user["id"], values)
                flash("Perfil guardado. Puede actualizarlo cuando lo necesite.", "success")
                return redirect(target or url_for("auth.profile"))
        else:
            values = accounts.get_profile(store(), g.current_user["id"])
    except SQLAlchemyError:
        return _service_unavailable()
    return render_template("auth/profile.html", errors=errors, values=values, next_url=target), (400 if errors else 200)
