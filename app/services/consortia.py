"""HU-012: consorcios entre empresas registradas.

Una empresa (la líder) crea el consorcio e invita a otras por el correo de su cuenta. La invitada
solo entra al consorcio si acepta: hasta entonces la líder no ve sus datos, y la aplicación nunca
confirma si un correo tiene cuenta. Las integrantes que aceptaron ven el aporte de cada una.

La suma de capacidades y su comparación con lo que piden las bases son funciones puras
(combine, evaluate), probadas sin base de datos.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from services.accounts import AccountBase, Company, CompanyProfile, User, now

MAX_PARTNERS = 9            # integrantes además de la líder (10 en total)
MAX_CONSORTIA = 20          # consorcios que una empresa puede liderar
NAME_MAX = 120
PENDING, ACCEPTED, REJECTED = "pendiente", "aceptada", "rechazada"
NO_STAFF = re.compile(r"^\s*no\s+(cuento|tengo|tenemos|contamos)\b", re.IGNORECASE)
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class ConsortiumError(ValueError):
    """Mensaje apto para mostrar al usuario."""


class Consortium(AccountBase):
    __tablename__ = "consorcios"
    id: Mapped[int] = mapped_column(primary_key=True)
    leader_company_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConsortiumMember(AccountBase):
    """Invitación a un consorcio. company_id se completa cuando la empresa invitada acepta."""
    __tablename__ = "consorcio_integrantes"
    __table_args__ = (UniqueConstraint("consortium_id", "invited_email", name="uq_consorcio_correo"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    consortium_id: Mapped[int] = mapped_column(ForeignKey("consorcios.id"), index=True)
    invited_email: Mapped[str] = mapped_column(String(254), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("empresas.id"), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default=PENDING)
    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Suma de capacidades y evaluación (sin base de datos)
# ---------------------------------------------------------------------------

@dataclass
class Member:
    """Capacidades declaradas por una empresa en su perfil (HU-008)."""
    company_id: int
    name: str
    role: str = "integrante"                 # "lider" o "integrante"
    has_profile: bool = True
    monthly_sales: Decimal = Decimal("0")
    similar_contracts_amount: Decimal = Decimal("0")
    experience_years: int = 0
    staff: str = ""
    member_id: int | None = None             # fila de consorcio_integrantes (la líder no tiene)
    staff_lines: list = field(default_factory=list)

    def __post_init__(self):
        self.staff_lines = staff_lines(self.staff)

    @property
    def staff_count(self):
        return len(self.staff_lines)


def staff_lines(text):
    """Cada línea no vacía del campo «Personal especializado» cuenta como un perfil declarado."""
    lines = [re.sub(r"^\s*[-•*\d.)]+\s*", "", line).strip() for line in (text or "").splitlines()]
    return [line for line in lines if line and not NO_STAFF.match(line)]


# (clave, etiqueta, cómo se combina en el consorcio, tipo de valor)
CAPABILITIES = [
    ("monthly_sales", "Ventas mensuales", "suma", "monto"),
    ("similar_contracts_amount", "Monto facturado en contratos similares", "suma", "monto"),
    ("experience_years", "Años de experiencia", "máximo", "años"),
    ("staff_count", "Personal especializado declarado", "suma", "personas"),
]
LABELS = {key: label for key, label, _, _ in CAPABILITIES}


def value_of(member, key):
    return getattr(member, key)


def combine(members):
    """Capacidades del consorcio: suma de ventas, de monto facturado y de personal; los años de
    experiencia no se suman (se toma el mayor). Solo cuentan las integrantes con perfil completo."""
    counted = [m for m in members if m.has_profile]
    totals = {}
    for key, _, how, _ in CAPABILITIES:
        values = [value_of(m, key) for m in counted]
        if how == "suma":
            totals[key] = sum(values, Decimal("0") if key != "staff_count" else 0)
        else:
            totals[key] = max(values, default=0)
    return {"totals": totals, "counted": counted, "missing_profiles": [m.name for m in members if not m.has_profile]}


def evaluate(members, required, own_company_id):
    """Compara cada requisito con la empresa sola y con el consorcio, con el aporte de cada integrante.

    required: {clave: valor exigido} solo con los requisitos que el usuario indicó.
    Devuelve None si no se indicó ningún requisito.
    """
    required = {k: v for k, v in required.items() if v is not None}
    if not required:
        return None
    combined = combine(members)
    totals, counted = combined["totals"], combined["counted"]
    own = next((m for m in members if m.company_id == own_company_id), None)
    rows = []
    for key, label, how, kind in CAPABILITIES:
        if key not in required:
            continue
        need = required[key]
        total = totals[key]
        contributions = []
        for m in counted:
            value = value_of(m, key)
            share = (float(value) / float(total) * 100) if (how == "suma" and total) else None
            contributions.append({"name": m.name, "role": m.role, "value": value, "share": share,
                                  "meets": value >= need, "own": m.company_id == own_company_id})
        own_value = value_of(own, key) if own and own.has_profile else None
        rows.append({"key": key, "label": label, "how": how, "kind": kind, "required": need,
                     "own_value": own_value, "own_meets": own_value is not None and own_value >= need,
                     "total": total, "total_meets": total >= need, "contributions": contributions})
    alone_meets = all(r["own_meets"] for r in rows)
    consortium_meets = all(r["total_meets"] for r in rows)
    # Ninguna integrante lo logra por separado: el consorcio es lo que lo hace posible
    nobody_alone = not any(all(c["meets"] for c in (r["contributions"][i] for r in rows))
                           for i in range(len(counted))) if counted else True
    return {"rows": rows, "alone_meets": alone_meets, "consortium_meets": consortium_meets,
            "nobody_alone": nobody_alone, "missing_profiles": combined["missing_profiles"],
            "own_has_profile": bool(own and own.has_profile)}


# ---------------------------------------------------------------------------
# Requisitos indicados por el usuario o leídos de las bases
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = {          # parámetro del formulario -> capacidad
    "ventas": "monthly_sales",
    "facturado": "similar_contracts_amount",
    "anios": "experience_years",
    "personal": "staff_count",
}


def parse_required(args):
    """Lee los requisitos del formulario (GET). Devuelve (valores para el formulario, requisitos, errores)."""
    values, required, errors = {}, {}, {}
    for param, key in REQUIRED_FIELDS.items():
        raw = (args.get(param) or "").strip()
        values[param] = raw
        if not raw:
            continue
        if key in ("monthly_sales", "similar_contracts_amount"):
            if re.fullmatch(r"\d{1,14}([.,]\d{1,2})?", raw):
                required[key] = Decimal(raw.replace(",", "."))
            else:
                errors[param] = "Escriba un monto sin separadores de miles y con hasta dos decimales."
        else:
            limit = 200 if key == "experience_years" else 999
            if re.fullmatch(r"\d{1,3}", raw) and int(raw) <= limit:
                required[key] = int(raw)
            else:
                errors[param] = f"Escriba un número entero de 0 a {limit}."
    return values, required, errors


AMOUNT_IN_BASES = re.compile(
    r"monto\s+(?:total\s+)?facturado(?:\s+acumulado)?[^.]{0,160}?S\s*/\s*\.?\s*([\d][\d\s.,]*\d)",
    re.IGNORECASE | re.DOTALL)


def parse_pen(raw):
    """«150,000.00», «150 000,00», «150.000» o «150000» -> Decimal."""
    text = re.sub(r"\s", "", raw)
    last_dot, last_comma = text.rfind("."), text.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        decimal_sep = "." if last_dot > last_comma else ","
    elif last_dot >= 0 or last_comma >= 0:
        pos = max(last_dot, last_comma)
        decimal_sep = text[pos] if len(text) - pos - 1 in (1, 2) else None
    else:
        decimal_sep = None
    if decimal_sep:
        whole, _, cents = text.rpartition(decimal_sep)
        text = re.sub(r"[.,]", "", whole) + "." + cents
    else:
        text = re.sub(r"[.,]", "", text)
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def amount_from_requirements(reqs):
    """Monto facturado que exigen las bases, con su página, si los requisitos lo dicen de forma
    reconocible («monto facturado acumulado equivalente a S/ 150,000.00»). None si no lo encuentra."""
    if not reqs or not getattr(reqs, "available", False):
        return None
    for block in reqs.blocks:
        match = AMOUNT_IN_BASES.search(block.text or "")
        if match:
            amount = parse_pen(match.group(1))
            if amount and amount > 0:
                excerpt = " ".join(match.group(0).split())
                return {"amount": amount, "page": block.page, "excerpt": excerpt}
    return None


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def normalize_email(email):
    return unicodedata.normalize("NFC", (email or "").strip().lower())


def _company(db, user_id):
    return db.scalar(select(Company).where(Company.user_id == user_id))


def _member_from_company(db, company, role):
    profile = db.get(CompanyProfile, company.id)
    if profile is None:
        return Member(company_id=company.id, name=company.name, role=role, has_profile=False)
    return Member(company_id=company.id, name=company.name, role=role, has_profile=True,
                  monthly_sales=Decimal(profile.monthly_sales), experience_years=int(profile.experience_years),
                  similar_contracts_amount=Decimal(profile.similar_contracts_amount),
                  staff=profile.specialized_staff or "")


def _role(db, consortium, company_id):
    if consortium.leader_company_id == company_id:
        return "lider"
    accepted = db.scalar(select(ConsortiumMember.id).where(
        ConsortiumMember.consortium_id == consortium.id, ConsortiumMember.company_id == company_id,
        ConsortiumMember.status == ACCEPTED))
    return "integrante" if accepted else None


def create(factory, user_id, name):
    name = " ".join((name or "").split())
    if not 3 <= len(name) <= NAME_MAX:
        raise ConsortiumError(f"Escriba un nombre de 3 a {NAME_MAX} caracteres.")
    with factory.begin() as db:
        company = _company(db, user_id)
        count = db.scalar(select(func.count()).select_from(Consortium)
                          .where(Consortium.leader_company_id == company.id))
        if count >= MAX_CONSORTIA:
            raise ConsortiumError(f"Su empresa ya lidera {MAX_CONSORTIA} consorcios. Elimine uno que ya no use.")
        consortium = Consortium(leader_company_id=company.id, name=name)
        db.add(consortium)
        db.flush()
        return consortium.id


def overview(factory, user_id, email):
    """Consorcios que la empresa lidera, a los que pertenece y las invitaciones que tiene pendientes."""
    email = normalize_email(email)
    with factory() as db:
        company = _company(db, user_id)
        partners = (select(ConsortiumMember.consortium_id, func.count().label("n"))
                    .where(ConsortiumMember.status == ACCEPTED).group_by(ConsortiumMember.consortium_id).subquery())
        led = db.execute(select(Consortium.id, Consortium.name, func.coalesce(partners.c.n, 0))
                         .outerjoin(partners, partners.c.consortium_id == Consortium.id)
                         .where(Consortium.leader_company_id == company.id)
                         .order_by(Consortium.created_at.desc(), Consortium.id.desc())).all()
        joined = db.execute(select(Consortium.id, Consortium.name, Company.name)
                            .join(ConsortiumMember, ConsortiumMember.consortium_id == Consortium.id)
                            .join(Company, Company.id == Consortium.leader_company_id)
                            .where(ConsortiumMember.company_id == company.id, ConsortiumMember.status == ACCEPTED)
                            .order_by(Consortium.name)).all()
        invitations = db.execute(select(ConsortiumMember.id, Consortium.name, Company.name)
                                 .join(Consortium, Consortium.id == ConsortiumMember.consortium_id)
                                 .join(Company, Company.id == Consortium.leader_company_id)
                                 .where(ConsortiumMember.invited_email == email, ConsortiumMember.status == PENDING,
                                        Consortium.leader_company_id != company.id)
                                 .order_by(ConsortiumMember.invited_at)).all()
        return {
            "company_id": company.id,
            "led": [{"id": i, "name": n, "members": c + 1} for i, n, c in led],
            "joined": [{"id": i, "name": n, "leader": leader} for i, n, leader in joined],
            "invitations": [{"id": i, "name": n, "leader": leader} for i, n, leader in invitations],
        }


def detail(factory, user_id, consortium_id):
    """Consorcio con sus integrantes, solo para la líder y las integrantes que aceptaron; si no, None."""
    with factory() as db:
        company = _company(db, user_id)
        consortium = db.get(Consortium, consortium_id)
        if consortium is None:
            return None
        role = _role(db, consortium, company.id)
        if role is None:
            return None
        leader = db.get(Company, consortium.leader_company_id)
        members = [_member_from_company(db, leader, "lider")]
        invitations = []
        rows = db.scalars(select(ConsortiumMember).where(ConsortiumMember.consortium_id == consortium.id)
                          .order_by(ConsortiumMember.invited_at, ConsortiumMember.id)).all()
        for row in rows:
            if row.status == ACCEPTED and row.company_id:
                member = _member_from_company(db, db.get(Company, row.company_id), "integrante")
                member.member_id = row.id
                members.append(member)
            elif role == "lider":           # los correos invitados solo los ve la líder
                invitations.append({"id": row.id, "email": row.invited_email, "status": row.status})
        return {"id": consortium.id, "name": consortium.name, "role": role, "company_id": company.id,
                "members": members, "invitations": invitations,
                "slots_left": MAX_PARTNERS - len([r for r in rows if r.status != REJECTED])}


def invite(factory, user_id, consortium_id, email):
    """Solo la líder invita. No revela si el correo tiene cuenta: la invitación queda pendiente igual."""
    email = normalize_email(email)
    if len(email) > 254 or not EMAIL.fullmatch(email):
        raise ConsortiumError("Escriba un correo válido.")
    with factory.begin() as db:
        company = _company(db, user_id)
        consortium = db.get(Consortium, consortium_id)
        if consortium is None or consortium.leader_company_id != company.id:
            return False
        leader_email = db.scalar(select(User.email).where(User.id == user_id))
        if email == normalize_email(leader_email):
            raise ConsortiumError("Ese es el correo de su propia cuenta: su empresa ya es la líder del consorcio.")
        rows = db.scalars(select(ConsortiumMember).where(ConsortiumMember.consortium_id == consortium.id)).all()
        existing = next((r for r in rows if r.invited_email == email), None)
        if existing and existing.status != REJECTED:
            raise ConsortiumError("Ese correo ya está invitado o ya integra el consorcio.")
        if len([r for r in rows if r.status != REJECTED]) >= MAX_PARTNERS:
            raise ConsortiumError(f"El consorcio ya tiene el máximo de {MAX_PARTNERS + 1} empresas.")
        if existing:                       # se vuelve a invitar a quien había rechazado
            existing.status, existing.company_id, existing.invited_at, existing.answered_at = PENDING, None, now(), None
        else:
            db.add(ConsortiumMember(consortium_id=consortium.id, invited_email=email))
        return True


def respond(factory, user_id, email, member_id, accept):
    """La empresa invitada acepta o rechaza. Solo la cuenta con ese correo puede responder."""
    email = normalize_email(email)
    with factory.begin() as db:
        company = _company(db, user_id)
        row = db.get(ConsortiumMember, member_id)
        if row is None or row.invited_email != email or row.status != PENDING:
            return None
        consortium = db.get(Consortium, row.consortium_id)
        if consortium.leader_company_id == company.id:
            return None
        row.status = ACCEPTED if accept else REJECTED
        row.company_id = company.id if accept else None
        row.answered_at = now()
        return consortium.id


def remove(factory, user_id, consortium_id, member_id):
    """La líder quita a una integrante o cancela una invitación; una integrante puede retirarse."""
    with factory.begin() as db:
        company = _company(db, user_id)
        consortium = db.get(Consortium, consortium_id)
        row = db.get(ConsortiumMember, member_id)
        if consortium is None or row is None or row.consortium_id != consortium.id:
            return None
        if consortium.leader_company_id == company.id:
            db.delete(row)
            return "quitada"
        if row.company_id == company.id and row.status == ACCEPTED:
            db.delete(row)
            return "retirada"
        return None


def delete_consortium(factory, user_id, consortium_id):
    with factory.begin() as db:
        company = _company(db, user_id)
        consortium = db.get(Consortium, consortium_id)
        if consortium is None or consortium.leader_company_id != company.id:
            return False
        db.execute(delete(ConsortiumMember).where(ConsortiumMember.consortium_id == consortium.id))
        db.delete(consortium)
        return True