"""HU-008: cuentas, empresas, perfiles y sesiones. Comparte Neon con la ingesta,
pero crea únicamente sus propias tablas; no modifica las tablas de EN-007/EN-012.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import secrets
import re

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash


def now():
    return datetime.now(timezone.utc)


class AccountBase(DeclarativeBase):
    pass


class User(AccountBase):
    __tablename__ = "usuarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Company(AccountBase):
    __tablename__ = "empresas"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Las cuentas anteriores conservan NULL hasta completar su perfil; no se inventa un RUC.
    ruc: Mapped[str | None] = mapped_column(String(11), nullable=True, unique=True, index=True)


class CompanyProfile(AccountBase):
    __tablename__ = "perfiles_empresa"
    company_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), primary_key=True)
    business_activity: Mapped[str] = mapped_column(String(300))
    rubros: Mapped[str] = mapped_column(String(500))
    monthly_sales: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    experience_years: Mapped[int] = mapped_column(Integer)
    similar_contracts_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    specialized_staff: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CompanyProduct(AccountBase):
    """Último bien confirmado; el costo y la descripción libre no se almacenan."""
    __tablename__ = "productos_empresa"
    company_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), primary_key=True)
    catalog_code: Mapped[int] = mapped_column(Integer)
    profile_key: Mapped[str] = mapped_column(String(64))


class LoginSession(AccountBase):
    __tablename__ = "sesiones_usuario"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginAttempt(AccountBase):
    __tablename__ = "intentos_acceso"
    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class EmailTaken(ValueError):
    pass


class RucTaken(ValueError):
    pass


def valid_ruc(value):
    """Valida el formato acordado; no acredita inscripción ni estado en SUNAT."""
    return isinstance(value, str) and re.fullmatch(r"[0-9]{11}", value) is not None


def initialize_schema(engine):
    """Migración aditiva e idempotente de cuentas existentes en SQLite o PostgreSQL."""
    AccountBase.metadata.create_all(engine)
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("empresas")}
        if "ruc" not in columns:
            optional = " IF NOT EXISTS" if engine.dialect.name == "postgresql" else ""
            connection.exec_driver_sql(f"ALTER TABLE empresas ADD COLUMN{optional} ruc VARCHAR(11)")
        # El índice impide duplicados incluso ante dos guardados simultáneos.
        connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_empresas_ruc ON empresas (ruc)")


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(factory, email, password, company_name):
    """Crea usuario y empresa en una transacción. Nunca almacena la contraseña recibida."""
    hashed = generate_password_hash(password, method="scrypt")
    try:
        with factory.begin() as db:
            user = User(email=email, password_hash=hashed)
            db.add(user)
            db.flush()
            db.add(Company(user_id=user.id, name=company_name))
            return user.id
    except IntegrityError as error:
        raise EmailTaken("Ya existe una cuenta con ese correo.") from error


# Se comprueba un hash también para correos inexistentes, evitando una salida inmediata.
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(24), method="scrypt")


def authenticate(factory, email, password):
    with factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        valid = check_password_hash(user.password_hash if user else _DUMMY_HASH, password)
        return user.id if user and valid else None


def record_attempt(factory, fingerprint):
    """Hasta ocho intentos por dirección y correo en quince minutos, compartidos entre workers."""
    cutoff = now() - timedelta(minutes=15)
    with factory.begin() as db:
        db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
        count = db.scalar(select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.fingerprint == fingerprint, LoginAttempt.created_at >= cutoff))
        if count >= 8:
            return False
        db.add(LoginAttempt(fingerprint=fingerprint))
        return True


def begin_session(factory, user_id):
    token = secrets.token_urlsafe(32)
    with factory.begin() as db:
        db.execute(delete(LoginSession).where(LoginSession.expires_at <= now()))
        db.add(LoginSession(token_hash=digest(token), user_id=user_id,
                            expires_at=now() + timedelta(hours=12)))
    return token


def end_session(factory, token):
    if token:
        with factory.begin() as db:
            db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(token)))


def current_account(factory, token):
    if not isinstance(token, str) or len(token) > 128:
        return None
    with factory() as db:
        row = db.execute(select(User.id, User.email, Company.id, Company.name)
                         .join(Company, Company.user_id == User.id)
                         .join(LoginSession, LoginSession.user_id == User.id)
                         .where(LoginSession.token_hash == digest(token),
                                LoginSession.expires_at > now())).first()
        return {"id": row[0], "email": row[1], "company_id": row[2], "company_name": row[3]} if row else None


def get_profile(factory, user_id):
    """La empresa se resuelve desde el usuario autenticado, nunca desde el formulario o la URL."""
    with factory() as db:
        company = db.scalar(select(Company).where(Company.user_id == user_id))
        profile = db.get(CompanyProfile, company.id)
        values = {"company_name": company.name, "ruc": company.ruc or ""}
        if profile:
            for field in ("business_activity", "rubros", "monthly_sales", "experience_years",
                          "similar_contracts_amount", "specialized_staff"):
                values[field] = getattr(profile, field)
        return values


def _product_profile_key(profile):
    values = (getattr(profile, key, "") or "" for key in ("business_activity", "rubros"))
    return digest("\x1f".join(value.strip() for value in values))


def get_company_product(factory, user_id):
    """Recupera el bien de la cuenta; cambiar giro o rubros exige confirmar de nuevo."""
    with factory() as db:
        company = db.scalar(select(Company).where(Company.user_id == user_id))
        product = db.get(CompanyProduct, company.id)
        profile = db.get(CompanyProfile, company.id)
        if product and product.profile_key == _product_profile_key(profile):
            return product.catalog_code
    return None


def remember_company_product(factory, user_id, catalog_code):
    """Asocia el código confirmado al usuario autenticado, nunca a un ID del formulario."""
    with factory.begin() as db:
        company = db.scalar(select(Company).where(Company.user_id == user_id))
        profile = db.get(CompanyProfile, company.id)
        product = db.get(CompanyProduct, company.id)
        if product is None:
            product = CompanyProduct(company_id=company.id)
            db.add(product)
        product.catalog_code = catalog_code
        product.profile_key = _product_profile_key(profile)


def save_profile(factory, user_id, values):
    if "ruc" in values and not valid_ruc(values["ruc"]):
        raise ValueError("El RUC debe contener exactamente 11 dígitos.")
    try:
        with factory.begin() as db:
            company = db.scalar(select(Company).where(Company.user_id == user_id))
            if "ruc" in values:
                duplicate = db.scalar(select(Company.id).where(Company.ruc == values["ruc"], Company.id != company.id))
                if duplicate is not None:
                    raise RucTaken("Este RUC ya está asociado a otra cuenta.")
                company.ruc = values["ruc"]
            company.name = values["company_name"]
            profile = db.get(CompanyProfile, company.id)
            if profile is None:
                profile = CompanyProfile(company_id=company.id)
                db.add(profile)
            for key in ("business_activity", "rubros", "monthly_sales", "experience_years",
                        "similar_contracts_amount", "specialized_staff"):
                setattr(profile, key, values[key])
    except IntegrityError as error:
        if "ruc" in str(error.orig).lower():
            raise RucTaken("Este RUC ya está asociado a otra cuenta.") from error
        raise
