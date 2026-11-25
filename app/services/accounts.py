"""HU-008: cuentas, empresas, perfiles y sesiones. Comparte Neon con la ingesta,
pero crea únicamente sus propias tablas; no modifica las tablas de EN-007/EN-012.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import hashlib
import secrets
import re

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, delete, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from services.ruc_lookup import RucDetails


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
    # 19/11/2026 (asesor): quién postula y qué ofrece. Vacíos en los perfiles anteriores.
    supplier_type: Mapped[str | None] = mapped_column(String(20), nullable=True)   # SUPPLIER_TYPES
    categories: Mapped[str | None] = mapped_column(String(40), nullable=True)      # "bienes servicios obras"
    rnp_registers: Mapped[str | None] = mapped_column(String(80), nullable=True)   # RNP_REGISTERS, separados por espacio
    # 20/11/2026: dato de la empresa, no de cada convocatoria (antes se pedía en cada ficha)
    is_mype: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 23/11/2026: cuánto del monto facturado en contratos similares es de cada objeto. Las bases piden
    # experiencia en bienes, servicios u obras «iguales o similares»: un solo total no distingue.
    similar_amount_bienes: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    similar_amount_servicios: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    similar_amount_obras: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# Quién postula. El consorcio no es un tipo de cuenta: se forma en /consorcio con otras cuentas.
SUPPLIER_TYPES = {"empresa": "Empresa (persona jurídica)",
                  "persona_natural": "Persona natural (profesional o especialista independiente)"}
CATEGORY_LABELS = {"bienes": "Bienes", "servicios": "Servicios", "obras": "Obras"}
# Registros del RNP según la Directiva de procedimientos y trámites ante el RNP del OECE (2025):
# proveedor de bienes, proveedor de servicios, ejecutor de obras y consultor de obras.
RNP_REGISTERS = {"bienes": "Proveedor de bienes", "servicios": "Proveedor de servicios",
                 "ejecutor_obras": "Ejecutor de obras", "consultor_obras": "Consultor de obras"}
PROFILE_EXTRA_COLUMNS = {"supplier_type": "VARCHAR(20)", "categories": "VARCHAR(40)", "rnp_registers": "VARCHAR(80)",
                         "is_mype": "BOOLEAN", "similar_amount_bienes": "NUMERIC(16, 2)",
                         "similar_amount_servicios": "NUMERIC(16, 2)", "similar_amount_obras": "NUMERIC(16, 2)"}
# Campo del monto por objeto (opcional) en Mi empresa
SIMILAR_BY_CATEGORY = {"bienes": "similar_amount_bienes", "servicios": "similar_amount_servicios",
                       "obras": "similar_amount_obras"}


def similar_for_category(total, by_category, offered, category):
    """(monto facturado en contratos similares para un objeto, ¿cómo se obtuvo?).

    Cómo: "declarado" si Mi empresa lo indicó; "resto" si es el único objeto que ofrece sin monto y los demás sí
    lo tienen (total menos lo declarado en los otros, 23/11/2026); "total" si ofrece solo ese objeto o no dijo
    qué ofrece; "sin separar" si ofrece varios y no lo separó (el total no confirma ese objeto).
    """
    if by_category.get(category) is not None:
        return by_category[category], "declarado"
    others = [c for c in offered if c != category]
    if total is not None and others and category in offered and all(by_category.get(c) is not None for c in others):
        declared = sum((v for c, v in by_category.items() if c != category and v is not None), Decimal("0"))
        return max(total - declared, Decimal("0")), "resto"
    return total, "sin separar" if len(offered) > 1 else "total"


def split_choices(text, allowed):
    """'bienes obras' → ['bienes', 'obras'] en el orden de allowed; ignora lo desconocido."""
    found = set(str(text or "").split())
    return [key for key in allowed if key in found]


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


class PasswordReset(AccountBase):
    __tablename__ = "recuperaciones_contrasena"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    password_fingerprint: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RecoveryThrottle(AccountBase):
    __tablename__ = "limites_recuperacion"
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CompanyRucLookup(AccountBase):
    __tablename__ = "empresas_ruc_consultas"
    company_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), primary_key=True)
    ruc: Mapped[str] = mapped_column(String(11))
    company_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(64))
    condition: Mapped[str] = mapped_column(String(64))


class RucLookupQuota(AccountBase):
    __tablename__ = "limites_consulta_ruc"
    scope: Mapped[str] = mapped_column(String(40), primary_key=True)
    day: Mapped[str] = mapped_column(String(10))
    attempts: Mapped[int] = mapped_column(Integer)


class _RucLookupLimit(Exception):
    pass

class EmailTaken(ValueError):
    pass


class RucTaken(ValueError):
    pass


def valid_ruc(value):
    """Valida el formato acordado; no acredita inscripción ni estado en SUNAT."""
    return isinstance(value, str) and re.fullmatch(r"[0-9]{11}", value) is not None


def initialize_schema(engine, *, password_management=False, ruc_lookup_enabled=False):
    """Migración aditiva e idempotente de cuentas existentes en SQLite o PostgreSQL."""
    from services import company_documents, staff  # noqa: F401  (registra sus tablas en AccountBase)

    optional = set()
    if not password_management:
        optional.update((PasswordReset.__table__, RecoveryThrottle.__table__))
    if not ruc_lookup_enabled:
        optional.update((CompanyRucLookup.__table__, RucLookupQuota.__table__))
    tables = [table for table in AccountBase.metadata.sorted_tables if table not in optional]
    AccountBase.metadata.create_all(engine, tables=tables)
    with engine.begin() as connection:
        # Perfil: tipo de proveedor, categorías y registros del RNP (19/11/2026). Columnas que admiten vacío.
        profile_columns = {column["name"] for column in inspect(connection).get_columns("perfiles_empresa")}
        for name, kind in PROFILE_EXTRA_COLUMNS.items():
            if name not in profile_columns:
                connection.exec_driver_sql(f"ALTER TABLE perfiles_empresa ADD COLUMN {name} {kind}")
        # Documentos del personal clave (20/11/2026): a quién pertenecen y qué se leyó de ellos.
        document_columns = {column["name"] for column in inspect(connection).get_columns("documentos_empresa")}
        for name, kind in company_documents.STAFF_COLUMNS.items():
            if name not in document_columns:
                connection.exec_driver_sql(f"ALTER TABLE documentos_empresa ADD COLUMN {name} {kind}")
        columns = {column["name"] for column in inspect(connection).get_columns("empresas")}
        if "ruc" not in columns:
            optional = " IF NOT EXISTS" if engine.dialect.name == "postgresql" else ""
            connection.exec_driver_sql(f"ALTER TABLE empresas ADD COLUMN{optional} ruc VARCHAR(11)")
        # No se borran ni reasignan RUC de bases anteriores que admitían duplicados.
        duplicate = connection.exec_driver_sql(
            "SELECT 1 FROM empresas WHERE ruc IS NOT NULL AND ruc <> '' GROUP BY ruc HAVING COUNT(*) > 1 LIMIT 1"
        ).first()
        if duplicate:
            logging.getLogger(__name__).warning(
                "Hay RUC duplicados anteriores: revise la migración antes de crear el índice único.")
        else:
            connection.exec_driver_sql("UPDATE empresas SET ruc = NULL WHERE ruc = ''")
            connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_empresas_ruc ON empresas (ruc)")


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def recovery_attempt(factory, fingerprint, limit):
    """Contador compartido y atómico por ventana; no bloquea el inicio de sesión."""
    moment = now()
    with factory.begin() as db:
        db.execute(delete(RecoveryThrottle).where(RecoveryThrottle.expires_at <= moment))
        count = db.scalar(update(RecoveryThrottle).where(
            RecoveryThrottle.fingerprint == fingerprint).values(
                attempts=RecoveryThrottle.attempts + 1).returning(RecoveryThrottle.attempts))
        if count is not None:
            return count <= limit
        # El savepoint resuelve dos primeras solicitudes simultáneas sin perder la transacción.
        try:
            with db.begin_nested():
                db.add(RecoveryThrottle(fingerprint=fingerprint, attempts=1,
                                       expires_at=moment + timedelta(minutes=15)))
                db.flush()
            return True
        except IntegrityError:
            count = db.scalar(update(RecoveryThrottle).where(
                RecoveryThrottle.fingerprint == fingerprint).values(
                    attempts=RecoveryThrottle.attempts + 1).returning(RecoveryThrottle.attempts))
            return count is not None and count <= limit


def issue_password_reset(factory, email):
    """Solo guarda el hash del secreto; pedir otro enlace no invalida los anteriores."""
    token = secrets.token_urlsafe(32)
    with factory.begin() as db:
        db.execute(delete(PasswordReset).where(PasswordReset.expires_at <= now()))
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            return None
        db.add(PasswordReset(token_hash=digest(token), user_id=user.id,
                             password_fingerprint=digest(user.password_hash),
                             expires_at=now() + timedelta(minutes=30)))
    return token


def revoke_password_reset(factory, token):
    with factory.begin() as db:
        db.execute(delete(PasswordReset).where(PasswordReset.token_hash == digest(token)))


def reset_password(factory, token, password):
    if not isinstance(token, str) or not 20 <= len(token) <= 128:
        return False
    hashed = generate_password_hash(password, method="scrypt")
    with factory.begin() as db:
        row = db.execute(select(PasswordReset.user_id, PasswordReset.password_fingerprint).where(
            PasswordReset.token_hash == digest(token), PasswordReset.expires_at > now()
        )).first()
        if not row:
            return False
        user = db.get(User, row.user_id)
        if not user or not secrets.compare_digest(row.password_fingerprint, digest(user.password_hash)):
            return False
        # Primero se actualiza el usuario con compare-and-swap. Así dos enlaces
        # del mismo usuario no toman bloqueos de tokens en orden inverso en PostgreSQL.
        active = select(PasswordReset.token_hash).where(
            PasswordReset.token_hash == digest(token), PasswordReset.expires_at > now()).exists()
        changed = db.execute(update(User).where(User.id == user.id, User.password_hash == user.password_hash, active)
                             .values(password_hash=hashed)).rowcount
        if not changed:
            return False
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        db.execute(delete(PasswordReset).where(PasswordReset.user_id == user.id))
    return True


def change_password(factory, user_id, current_password, new_password):
    with factory() as db:
        user = db.get(User, user_id)
        previous = user.password_hash if user else _DUMMY_HASH
    if not user or not check_password_hash(previous, current_password):
        return False
    hashed = generate_password_hash(new_password, method="scrypt")
    with factory.begin() as db:
        changed = db.execute(update(User).where(User.id == user_id, User.password_hash == previous)
                             .values(password_hash=hashed)).rowcount
        if not changed:
            return False
        db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
        db.execute(delete(PasswordReset).where(PasswordReset.user_id == user_id))
    return True


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
            values["supplier_type"] = profile.supplier_type or ""
            values["categories"] = split_choices(profile.categories, CATEGORY_LABELS)
            values["rnp_registers"] = split_choices(profile.rnp_registers, RNP_REGISTERS)
            values["is_mype"] = bool(profile.is_mype)
            for field in SIMILAR_BY_CATEGORY.values():
                values[field] = getattr(profile, field)
        return values


def _saved_ruc_details(db, company):
    row = db.get(CompanyRucLookup, company.id)
    if row is not None and row.ruc == company.ruc and row.company_name == company.name:
        return RucDetails(row.ruc, row.company_name, row.status, row.condition)
    return None


def get_saved_ruc_details(factory, user_id):
    with factory() as db:
        company = db.scalar(select(Company).where(Company.user_id == user_id))
        return _saved_ruc_details(db, company)


def reserve_ruc_lookup(factory, user_id, global_limit):
    """Reserva un intento por cuenta y para el servicio; el cupo se comparte entre procesos."""
    today = now().date().isoformat()
    reserve = text("""
        INSERT INTO limites_consulta_ruc (scope, day, attempts) VALUES (:scope, :day, 1)
        ON CONFLICT (scope) DO UPDATE SET
            day = excluded.day,
            attempts = CASE WHEN limites_consulta_ruc.day = excluded.day
                            THEN limites_consulta_ruc.attempts + 1 ELSE 1 END
        WHERE limites_consulta_ruc.day != excluded.day OR limites_consulta_ruc.attempts < :limit
        RETURNING attempts
    """)
    try:
        with factory.begin() as db:
            for scope, limit in ((f"cuenta:{user_id}", 5), ("global", global_limit)):
                if db.scalar(reserve, {"scope": scope, "day": today, "limit": limit}) is None:
                    raise _RucLookupLimit
    except _RucLookupLimit:
        return False
    return True


def save_ruc(factory, user_id, ruc):
    """Guarda un RUC opcional sin sustituir el existente ni tomar el de otra cuenta."""
    if not valid_ruc(ruc):
        return False
    try:
        with factory.begin() as db:
            company = db.scalar(select(Company).where(Company.user_id == user_id))
            if company is None or company.ruc:
                return False
            if db.scalar(select(Company.id).where(Company.ruc == ruc)) is not None:
                return False
            company.ruc = ruc
        return True
    except IntegrityError:
        return False


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


def save_profile(factory, user_id, values, *, ruc_lookup_enabled=False, ruc_details=None):
    if values.get("ruc") and not valid_ruc(values["ruc"]):
        raise ValueError("El RUC debe contener exactamente 11 dígitos.")
    try:
        with factory.begin() as db:
            company = db.scalar(select(Company).where(Company.user_id == user_id))
            saved_details = _saved_ruc_details(db, company) if ruc_lookup_enabled else None
            if "ruc" in values:
                duplicate = db.scalar(select(Company.id).where(Company.ruc == values["ruc"], Company.id != company.id))
                if values["ruc"] and duplicate is not None:
                    raise RucTaken("Este RUC ya está asociado a otra cuenta.")
                company.ruc = values["ruc"] or None
            proposed = ruc_details or saved_details
            if ruc_lookup_enabled:
                row = db.get(CompanyRucLookup, company.id)
                if proposed is not None and proposed.ruc == company.ruc:
                    if row is None:
                        row = CompanyRucLookup(company_id=company.id)
                        db.add(row)
                    row.ruc, row.company_name = proposed.ruc, proposed.company_name
                    row.status, row.condition = proposed.status, proposed.condition
                else:
                    if row is not None:
                        db.delete(row)
                    proposed = None
            company.name = proposed.company_name if ruc_lookup_enabled and proposed else values["company_name"]
            profile = db.get(CompanyProfile, company.id)
            if profile is None:
                profile = CompanyProfile(company_id=company.id)
                db.add(profile)
            for key in ("business_activity", "rubros", "monthly_sales", "experience_years",
                        "similar_contracts_amount", "specialized_staff"):
                setattr(profile, key, values[key])
            # Opcionales: un formulario anterior que no los envía no los borra.
            if "supplier_type" in values:
                profile.supplier_type = values["supplier_type"] if values["supplier_type"] in SUPPLIER_TYPES else None
            if "categories" in values:
                profile.categories = " ".join(split_choices(" ".join(values["categories"]), CATEGORY_LABELS)) or None
            if "rnp_registers" in values:
                profile.rnp_registers = " ".join(split_choices(" ".join(values["rnp_registers"]), RNP_REGISTERS)) or None
            if "is_mype" in values:
                profile.is_mype = bool(values["is_mype"])
            for field in SIMILAR_BY_CATEGORY.values():
                if field in values:
                    setattr(profile, field, values[field])
    except IntegrityError as error:
        if "ruc" in str(error.orig).lower():
            raise RucTaken("Este RUC ya está asociado a otra cuenta.") from error
        raise


def ensure_columns(engine):
    """Compatibilidad con el arranque y las migraciones aditivas de main."""
    initialize_schema(engine)
