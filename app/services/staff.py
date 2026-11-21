"""Personal clave y especialistas de la empresa (pedido del asesor, 19/11/2026).

En servicios y sobre todo en obras, las bases piden personal clave con una profesión, colegiatura y una
experiencia mínima (por ejemplo, «Residente de obra: ingeniero civil colegiado y habilitado, con 24 meses
de experiencia como residente»). La empresa registra aquí a sus profesionales una sola vez y el semáforo
(services/eligibility.py) los contrasta con lo que dicen las bases.

Son datos declarados por la empresa: no se verifica la colegiatura en el colegio profesional ni la
experiencia en los certificados. No se guarda el DNI ni otros datos personales que el semáforo no necesita.

Fichas técnicas y sustentos (20/11/2026, pedido del asesor: «comparar si realmente tiene la gente»): a cada
persona se le suben su currículum o ficha técnica, título, constancia de colegiatura y habilidad, constancias
de experiencia y certificados de capacitación. Se guardan como documentos de la empresa (tipo «Personal»,
services/company_documents.py) enlazados a la persona. Al subirlos se lee su texto en la computadora del
servidor, sin modelos de lenguaje: si mencionan la profesión declarada y, en la constancia de habilidad, la
colegiatura y su vigencia. El semáforo pide el sustento de quien cubre cada cargo.
"""
import re
import unicodedata

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from services import company_documents
from services.accounts import AccountBase, Company, now

MAX_STAFF = 60

# Profesiones que más piden las bases de obras y servicios. La clave es lo que se busca en el texto
# de las bases (sin tildes). La lista es para orientar el formulario: la empresa puede escribir otra.
PROFESSIONS = {
    "ingeniero civil": "Ingeniero civil",
    "ingeniero electricista": "Ingeniero electricista",
    "ingeniero mecanico electricista": "Ingeniero mecánico electricista",
    "ingeniero electronico": "Ingeniero electrónico",
    "ingeniero de telecomunicaciones": "Ingeniero de telecomunicaciones",
    "ingeniero de sistemas": "Ingeniero de sistemas",
    "ingeniero informatico": "Ingeniero informático",
    "ingeniero industrial": "Ingeniero industrial",
    "ingeniero mecanico": "Ingeniero mecánico",
    "ingeniero sanitario": "Ingeniero sanitario",
    "ingeniero ambiental": "Ingeniero ambiental",
    "ingeniero agricola": "Ingeniero agrícola",
    "ingeniero de minas": "Ingeniero de minas",
    "ingeniero geologo": "Ingeniero geólogo",
    "ingeniero de seguridad": "Ingeniero de seguridad",
    "arquitecto": "Arquitecto",
    "contador": "Contador",
    "abogado": "Abogado",
    "economista": "Economista",
    "administrador": "Administrador",
    "tecnico": "Técnico",
}

# Formas en que las bases escriben la misma profesión. Se comparan sin tildes.
ALIASES = {
    "ingeniero civil": ("ingeniero civil", "ing. civil", "ing civil"),
    "ingeniero electricista": ("ingeniero electricista", "ingeniero electrico", "ing. electricista", "ing. electrico"),
    "ingeniero mecanico electricista": ("ingeniero mecanico electricista", "ingeniero mecanico-electricista",
                                        "ing. mecanico electricista"),
    "ingeniero electronico": ("ingeniero electronico", "ing. electronico"),
    "ingeniero de telecomunicaciones": ("ingeniero de telecomunicaciones", "ingeniero en telecomunicaciones",
                                        "ingeniero electronico y de telecomunicaciones", "ing. de telecomunicaciones"),
    "ingeniero de sistemas": ("ingeniero de sistemas", "ingeniero en sistemas", "ing. de sistemas"),
    "ingeniero informatico": ("ingeniero informatico", "ingeniero de computacion", "ingeniero en computacion"),
    "ingeniero industrial": ("ingeniero industrial", "ing. industrial"),
    "ingeniero mecanico": ("ingeniero mecanico", "ing. mecanico"),
    "ingeniero sanitario": ("ingeniero sanitario", "ing. sanitario"),
    "ingeniero ambiental": ("ingeniero ambiental", "ing. ambiental"),
    "ingeniero agricola": ("ingeniero agricola", "ing. agricola"),
    "ingeniero de minas": ("ingeniero de minas",),
    "ingeniero geologo": ("ingeniero geologo",),
    "ingeniero de seguridad": ("ingeniero de seguridad", "ingeniero en seguridad"),
    "arquitecto": ("arquitecto",),
    "contador": ("contador publico", "contador"),
    "abogado": ("abogado",),
    "economista": ("economista",),
    "administrador": ("licenciado en administracion", "administrador"),
}


class StaffMember(AccountBase):
    __tablename__ = "personal_empresa"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    profession: Mapped[str] = mapped_column(String(120))      # «Ingeniero civil», «Técnico electricista»
    specialty: Mapped[str] = mapped_column(String(200), default="")   # «Telecomunicaciones», «Redes»
    licensed: Mapped[bool] = mapped_column(Boolean, default=False)     # colegiado y habilitado (declarado)
    experience_months: Mapped[int] = mapped_column(Integer, default=0)
    certifications: Mapped[str] = mapped_column(Text, default="")      # «PMP, ISO 9001…»
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StaffError(ValueError):
    """Mensaje apto para mostrar."""


FEMININE = {"ingeniera": "ingeniero", "arquitecta": "arquitecto", "contadora": "contador", "abogada": "abogado",
            "tecnica": "tecnico", "licenciada": "licenciado", "administradora": "administrador"}


def plain(text):
    """Minúsculas sin tildes, «ingeniero(a)» e «ingeniera» como «ingeniero», para comparar profesiones."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\((?:a|as|o|os)\)", "", text)
    text = re.sub(r"\b(" + "|".join(FEMININE) + r")\b", lambda m: FEMININE[m.group(1)], text)
    return re.sub(r"\s+", " ", text).strip()


def profession_key(text):
    """La clave de PROFESSIONS que corresponde a un texto libre, o None."""
    normalized = plain(text)
    # Primero las formas más largas: «ingeniero mecanico electricista» antes que «ingeniero mecanico»
    for key, forms in sorted(ALIASES.items(), key=lambda kv: -max(len(f) for f in kv[1])):
        if any(form in normalized for form in forms):
            return key
    return "tecnico" if re.search(r"\btecnic[oa]s?\b", normalized) else None


def validate(form):
    values = {key: (form.get(key) or "").strip() for key in ("name", "profession", "specialty", "certifications")}
    values["licensed"] = form.get("licensed") == "1"
    errors = {}
    if not 2 <= len(values["name"]) <= 120:
        errors["name"] = "Escriba el nombre (de 2 a 120 caracteres)."
    if not 2 <= len(values["profession"]) <= 120:
        errors["profession"] = "Escriba la profesión u oficio (por ejemplo, Ingeniero civil)."
    if len(values["specialty"]) > 200:
        errors["specialty"] = "Máximo 200 caracteres."
    if len(values["certifications"]) > 1000:
        errors["certifications"] = "Máximo 1000 caracteres."
    years, months = (form.get("years") or "0").strip(), (form.get("months") or "0").strip()
    values["years"], values["months"] = years, months
    if not re.fullmatch(r"\d{1,2}", years) or not re.fullmatch(r"\d{1,2}", months) or int(months) > 11:
        errors["experience"] = "Escriba los años (0 a 99) y los meses (0 a 11) de experiencia en el cargo o la especialidad."
    else:
        values["experience_months"] = int(years) * 12 + int(months)
    return values, errors


def _company_id(db, user_id):
    company = db.scalar(select(Company).where(Company.user_id == user_id))
    if company is None:
        raise StaffError("La cuenta no tiene empresa.")
    return company.id


def as_dict(member):
    return {"id": member.id, "name": member.name, "profession": member.profession, "specialty": member.specialty or "",
            "licensed": bool(member.licensed), "experience_months": member.experience_months or 0,
            "certifications": member.certifications or "", "profession_key": profession_key(member.profession)}


def list_staff(factory, user_id, on_date=None):
    """Personal de la empresa con sus documentos y el resumen de su sustento («evidence»).

    on_date: fecha en la que se mira la vigencia de los documentos (por defecto, hoy).
    """
    with factory() as db:
        company_id = _company_id(db, user_id)
        rows = db.scalars(select(StaffMember).where(StaffMember.company_id == company_id)
                          .order_by(StaffMember.profession, StaffMember.name)).all()
        members = [as_dict(r) for r in rows]
    documents = [d for d in company_documents.list_documents(factory, user_id, on_date) if d["staff_id"]]
    for member in members:
        member["documents"] = [d for d in documents if d["staff_id"] == member["id"]]
        member["evidence"] = evidence(member["documents"])
    return members


def evidence(documents):
    """Qué sustentos vigentes tiene una persona y qué documentos no coinciden con lo declarado."""
    current = [d for d in documents if d["state"] == "current"]
    return {"kinds": sorted({d["staff_kind"] for d in current if d["staff_kind"]}),
            "mismatch": [d["read_detail"] for d in current if d["read_status"] == "no_coincide" and d["read_detail"]],
            "expired": sorted({d["staff_kind"] for d in documents if d["state"] == "expired" and d["staff_kind"]})}


def missing_support(need, member):
    """Sustentos que faltan para que `member` acredite el cargo `need`. [] si no se lleva el control
    (miembro sin «evidence», por ejemplo en consorcios) o si está completo."""
    support = member.get("evidence")
    if support is None:
        return []
    kinds, missing = set(support["kinds"]), []
    if not kinds & {"cv", "titulo"}:
        missing.append("título o currículum")
    if need.get("licensed") and "colegiatura" not in kinds:
        missing.append("constancia de habilidad vigente" if "colegiatura" in support.get("expired", [])
                       else "constancia de colegiatura y habilidad")
    if need.get("months") and not kinds & {"experiencia", "cv"}:
        missing.append("constancias de experiencia")
    return missing


# ---------------------------------------------------------------------------
# Lectura de los documentos de una persona
# ---------------------------------------------------------------------------
LICENSE_NUMBER = re.compile(r"\b(?:c\.?\s?i\.?\s?p|c\.?\s?a\.?\s?p|colegiatura|registro\s+(?:del\s+)?colegio)\b"
                            r"[^0-9\n]{0,20}(\d{3,7})")
ENABLED = re.compile(r"\bhabilitad[oa]|\bhabil\b|constancia\s+de\s+habilidad|certificado\s+de\s+habilidad")
VALID_UNTIL = re.compile(r"(?:hasta|vigencia|vigente|vence|vencimiento)[^0-9\n]{0,40}(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")


def professions_in(text):
    """Claves de PROFESSIONS que aparecen en el texto (sin la genérica «técnico»)."""
    normalized = plain(text)
    return {key for key, forms in ALIASES.items() if any(form in normalized for form in forms)}


def read_document(text, kind, declared_profession):
    """{"status", "detail", "until"} de lo que dice un documento de una persona.

    status: «leido» (sin observaciones), «no_coincide» (menciona otra profesión, no la declarada),
    «sin_profesion» (no se encontró ninguna) o «sin_texto» (imagen escaneada: no se pudo leer).
    """
    if len(re.sub(r"\s+", "", text or "")) < 40:
        return {"status": "sin_texto", "until": None,
                "detail": "No se pudo leer el texto del documento; se guardó igual."}
    normalized, notes, status, until = plain(text), [], "leido", None
    declared = profession_key(declared_profession)
    label = PROFESSIONS.get(declared, declared_profession).lower() if declared else ""
    if kind in ("cv", "titulo", "colegiatura") and declared and declared != "tecnico":
        found = professions_in(text)
        if declared in found:
            notes.append(f"Menciona {label}.")
        elif found:
            status = "no_coincide"
            others = ", ".join(sorted(PROFESSIONS.get(k, k).lower() for k in found))
            notes.append(f"No menciona {label}; menciona {others}.")
        else:
            status = "sin_profesion"
            notes.append(f"No se encontró «{label}» en el documento.")
    if kind == "colegiatura":
        number = LICENSE_NUMBER.search(normalized)
        if number:
            notes.append(f"Colegiatura N.° {number.group(1)}.")
        if ENABLED.search(normalized):
            notes.append("Indica que está habilitado.")
        date_match = VALID_UNTIL.search(normalized)
        if date_match:
            try:
                until = datetime(int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1))).date()
                notes.append(f"Vigente hasta el {until.strftime('%d/%m/%Y')}.")
            except ValueError:
                until = None
    return {"status": status, "detail": " ".join(notes)[:300] or "Documento guardado.", "until": until}


def add_member_document(factory, user_id, member_id, kind, expires_on, pdf):
    """Guarda un documento de una persona de la propia empresa. Devuelve lo leído del documento.

    pdf: lo que devolvió company_documents.read_pdf (ya validado). Lanza StaffError o DocumentError.
    """
    if kind not in company_documents.STAFF_DOC_KINDS:
        raise StaffError("Elija qué documento es.")
    with factory() as db:
        company_id = _company_id(db, user_id)
        member = db.get(StaffMember, member_id)
        if member is None or member.company_id != company_id:
            raise StaffError("La persona no es de su empresa.")
        profession = member.profession
    reading = read_document(company_documents.read_text(pdf["content"]), kind, profession)
    if kind == "colegiatura" and expires_on is None:
        expires_on = reading["until"]            # la vigencia escrita en la constancia, si no se indicó
    metadata = {"document_type": "personal", "valid_from": None, "expires_on": expires_on}
    staff_fields = {"staff_id": member_id, "staff_kind": kind, "read_status": reading["status"],
                    "read_detail": reading["detail"]}
    company_documents.add_document(factory, user_id, metadata, pdf, staff=staff_fields)
    return reading


def add_member(factory, user_id, values):
    with factory.begin() as db:
        company_id = _company_id(db, user_id)
        count = db.scalar(select(func.count()).select_from(StaffMember).where(StaffMember.company_id == company_id))
        if count >= MAX_STAFF:
            raise StaffError(f"Puede registrar hasta {MAX_STAFF} personas.")
        member = StaffMember(company_id=company_id, name=values["name"], profession=values["profession"],
                             specialty=values["specialty"], licensed=values["licensed"],
                             experience_months=values["experience_months"], certifications=values["certifications"])
        db.add(member)
        db.flush()
        return member.id


def delete_member(factory, user_id, member_id):
    """Solo borra personal de la propia empresa. Devuelve False si no existe o es de otra empresa."""
    with factory.begin() as db:
        company_id = _company_id(db, user_id)
        member = db.get(StaffMember, member_id)
        if member is None or member.company_id != company_id:
            return False
        # Sus documentos se borran con la persona (20/11/2026)
        for document in db.scalars(select(company_documents.CompanyDocument).where(
                company_documents.CompanyDocument.company_id == company_id,
                company_documents.CompanyDocument.staff_id == member_id)):
            db.delete(document)
        db.delete(member)
        return True


def months_text(months):
    years, rest = divmod(int(months or 0), 12)
    parts = ([f"{years} año{'s' if years != 1 else ''}"] if years else []) + ([f"{rest} mes{'es' if rest != 1 else ''}"] if rest else [])
    return " y ".join(parts) or "sin experiencia declarada"


# ---------------------------------------------------------------------------
# Lectura de lo que piden las bases
# ---------------------------------------------------------------------------
NUMBER_WORDS = {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
                "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "doce": 12, "dieciocho": 18,
                "veinticuatro": 24, "treinta y seis": 36}


def _number(text):
    text = text.strip()
    return int(text) if text.isdigit() else NUMBER_WORDS.get(text)


def required_staff(text):
    """[{"profession", "label", "months", "licensed"}] de un bloque de requisitos.

    Solo devuelve lo que se puede leer con seguridad: una profesión conocida y, si aparece cerca (en los
    250 caracteres siguientes), la experiencia mínima en meses o años y si se pide colegiatura. Lo que no
    se reconoce queda para la revisión de la empresa (el semáforo no inventa requisitos).
    """
    normalized = plain(text)
    found, seen = [], set()
    forms = sorted(((form, key) for key, values in ALIASES.items() for form in values), key=lambda p: -len(p[0]))
    taken = []
    for form, key in forms:
        for match in re.finditer(re.escape(form), normalized):
            if any(start <= match.start() < end for start, end in taken):
                continue                                  # ya cubierto por una forma más larga
            taken.append((match.start(), match.end()))
            window = normalized[match.end():match.end() + 250]
            cut = re.search(r"\b(ingeniero|arquitecto|contador|abogado|economista)\b", window)
            window = window[:cut.start()] if cut else window
            number = r"(\d{1,3}|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")"
            months = None
            span = re.search(number + r"\s*(?:\(\d{1,3}\)\s*)?(meses|mes|anos|ano)\b", window)
            if span and _number(span.group(1)) is not None:
                value = _number(span.group(1))
                months = value if span.group(2).startswith("mes") else value * 12
            licensed = bool(re.search(r"colegiad|habilitad", window) or re.search(r"colegiad|habilitad",
                            normalized[max(0, match.start() - 60):match.start()]))
            item = (key, months, licensed)
            if item not in seen:
                seen.add(item)
                found.append({"profession": key, "label": PROFESSIONS.get(key, key.capitalize()),
                              "months": months, "licensed": licensed})
    return found


def match_requirements(required, members):
    """Para cada profesional exigido, la mejor persona registrada que cumple, o el motivo por el que no.

    Devuelve [{"required", "member" (o None), "problem" (o None)}]. Una persona no cubre dos cargos.
    """
    used, result = set(), []
    for need in sorted(required, key=lambda r: -(r["months"] or 0)):
        candidates = [m for m in members if m["profession_key"] == need["profession"] and m["id"] not in used]
        fitting = [m for m in candidates
                   if (need["months"] is None or m["experience_months"] >= need["months"])
                   and (not need["licensed"] or m["licensed"])]
        if fitting:
            # Entre las que cumplen, primero la que tiene su sustento completo (20/11/2026)
            best = max(fitting, key=lambda m: (not missing_support(need, m), m["experience_months"]))
            used.add(best["id"])
            result.append({"required": need, "member": best, "problem": None,
                           "missing": missing_support(need, best),
                           "mismatch": (best.get("evidence") or {}).get("mismatch", [])})
            continue
        if not candidates:
            problem = f"No registró a un {need['label'].lower()} en su personal."
        else:
            best = max(candidates, key=lambda m: m["experience_months"])
            if need["months"] is not None and best["experience_months"] < need["months"]:
                problem = (f"Su {need['label'].lower()} con más experiencia tiene {months_text(best['experience_months'])}; "
                           f"las bases piden {months_text(need['months'])}.")
            else:
                problem = f"Las bases piden un {need['label'].lower()} colegiado y habilitado; no lo declaró así."
        result.append({"required": need, "member": None, "problem": problem})
    return result