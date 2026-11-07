"""Rubros que descarga la ingesta (EN-011).

Un rubro es una familia UNSPSC: los 4 primeros dígitos del código del ítem (regla del SP-005),
por ejemplo 1510 (combustibles) o 2510 (vehículos). La ingesta descarga:

    rubros piloto (config.PILOT_RUBROS)  +  rubros solicitados activos (tabla "rubros_solicitados")

Con RUBROS_INGESTA=todos la ingesta guarda además todos los demás rubros de bienes de la fuente;
piloto y solicitados siguen teniendo prioridad en la lectura de bases.

Las empresas piden rubros en HU-013; mientras esa pantalla no exista, se registran con
`python ingest_job.py --solicitar-rubro 3011 --empresa "Nombre"`. Un rubro solicitado aparece
con sus convocatorias después de la siguiente ingesta. Si se desactiva, en la siguiente
ingesta sus convocatorias pasan a "no_vigente" (no se borran), salvo que también sean de otro rubro.
"""
import re

from sqlalchemy import select

import config
from services import db

# Bienes cuyos ítems no traen código de clasificación en la API (en la muestra de octubre del SP-005,
# 280 de 794 vigentes). Solo se guardan con RUBROS_INGESTA=todos; en el filtro y los conteos van con esta clave.
NO_RUBRO = "sin"
NO_RUBRO_LABEL = "Sin rubro (la API no publica su código)"

# Nombres de los rubros piloto para el filtro de la pantalla de convocatorias
PILOT_NAMES = {"1510": "Combustibles y lubricantes", "2510": "Vehículos de motor"}


class InvalidRubro(ValueError):
    """El texto no es un rubro: se esperan los 4 dígitos de una familia UNSPSC."""


def normalize_code(text):
    """'3011', '30111601' o '3011 1601' → '3011'. Lanza InvalidRubro si no hay al menos 4 dígitos."""
    digits = re.sub(r"\D", "", str(text or ""))
    if len(digits) < 4:
        raise InvalidRubro(f"«{text}» no es un rubro: escriba los 4 dígitos de la familia UNSPSC (por ejemplo 3011)")
    if digits[:4] == "0000":
        raise InvalidRubro(f"«{text}» no es un rubro válido")
    return digits[:4]


def request_rubro(session, code, company="", name=None):
    """Registra (o reactiva) el pedido de un rubro por una empresa. Devuelve la fila."""
    code = normalize_code(code)
    company = (company or "").strip()[:200]
    row = session.scalars(select(db.RubroRequest).where(db.RubroRequest.code == code)
                          .where(db.RubroRequest.company == company)).first()
    if row is None:
        row = db.RubroRequest(code=code, company=company)
        session.add(row)
    elif not row.active:
        row.requested_at = db.utcnow()          # se vuelve a pedir: cuenta desde hoy
    row.active = True
    if name:
        row.name = name.strip()[:200]
    session.flush()
    return row


def withdraw_rubro(session, code, company=None):
    """Desactiva el pedido (de una empresa, o de todas si company es None). Devuelve cuántos se desactivaron."""
    query = select(db.RubroRequest).where(db.RubroRequest.code == normalize_code(code)).where(db.RubroRequest.active)
    if company is not None:
        query = query.where(db.RubroRequest.company == company.strip())
    rows = list(session.scalars(query))
    for row in rows:
        row.active = False
    return len(rows)


def requested_codes(session):
    """Rubros solicitados activos (sin repetir), en orden."""
    return sorted(set(session.scalars(select(db.RubroRequest.code).where(db.RubroRequest.active))))


def ingestion_rubros(session):
    """Lista de rubros de la ingesta: primero los piloto, luego los solicitados que no son piloto."""
    pilots = list(dict.fromkeys(config.PILOT_RUBROS))
    return pilots + [c for c in requested_codes(session) if c not in pilots]


def requests_table(session):
    """[(código, nombre, empresa, fecha, activo)] para mostrar los pedidos."""
    rows = session.scalars(select(db.RubroRequest).order_by(db.RubroRequest.code, db.RubroRequest.requested_at))
    return [(r.code, r.name, r.company, r.requested_at, r.active) for r in rows]


def open_counts(session):
    """{rubro: convocatorias vigentes guardadas}, de mayor a menor."""
    from collections import Counter
    counts = Counter(code for text in session.scalars(select(db.Tender.rubros).where(db.Tender.state == "vigente"))
                     for code in (set((text or "").split()) or {NO_RUBRO}))
    return dict(counts.most_common())


def names(session):
    """{rubro: nombre}: el fijo de los piloto, si no el del catálogo, si no el que se dio al solicitarlo."""
    found = {}
    for code, name, _company, _at, active in requests_table(session):
        if active and name:
            found[code] = name
    for row in session.scalars(select(db.RubroCatalog)):
        if row.name:
            found[row.code] = row.name
    found.update(PILOT_NAMES)
    return found


LABEL_CHARS = 70          # el filtro es una lista desplegable: los nombres largos se cortan


def _label(code, all_names):
    name = all_names.get(code) or f"Rubro {code}"
    return f"{code} · {name if len(name) <= LABEL_CHARS else name[:LABEL_CHARS - 1].rstrip(' ,') + '…'}"


def named_options(session):
    """[(código, etiqueta)] de los rubros piloto y solicitados activos, con su nombre (sin conteos)."""
    all_names = names(session)
    return [(code, _label(code, all_names)) for code in ingestion_rubros(session)]


def _with_count(label, count):
    if not count:
        return f"{label} · sin datos"
    return f"{label} · {count} vigente{'s' if count != 1 else ''}"


def options(session):
    """[(código, etiqueta)] del filtro de la pantalla de convocatorias (HU-013): el catálogo completo.

    Orden: piloto y solicitados, luego el catálogo de mayor a menor, luego (con RUBROS_INGESTA=todos)
    los rubros con vigentes que no están en el catálogo, y al final los bienes sin rubro. Cada etiqueta
    dice cuántas vigentes hay guardadas o "sin datos" (entonces la pantalla ofrece solicitarlo).
    """
    all_names, counts = names(session), open_counts(session)
    codes = ingestion_rubros(session)
    codes += [r.code for r in catalog(session) if r.code not in codes]
    codes += [c for c in counts if c not in codes and c != NO_RUBRO]
    result = [(code, _with_count(_label(code, all_names), counts.get(code, 0))) for code in codes]
    if counts.get(NO_RUBRO):
        result.append((NO_RUBRO, _with_count(NO_RUBRO_LABEL, counts[NO_RUBRO])))
    return result


# ---------------------------------------------------------------------------
# Catálogo de rubros (HU-013)
# ---------------------------------------------------------------------------
CATALOG_DAYS = 30          # "el último mes": convocatorias de bienes publicadas en los últimos 30 días
NAME_EXAMPLES = 2          # cuántas descripciones UNSPSC forman el nombre del rubro


def catalog_from_rows(rows, now, days=CATALOG_DAYS):
    """[(rubro, nombre, convocatorias)] de los bienes publicados en los últimos `days` días, de mayor a menor.

    Cuenta todas las convocatorias de bienes (vigentes o no). La API no publica el nombre de la familia
    UNSPSC: el nombre se arma con las descripciones UNSPSC más frecuentes de sus ítems, por ejemplo
    "Microscopios invertidos, balanzas analíticas".
    """
    from collections import Counter, defaultdict
    from datetime import timedelta
    from services import ocds_source
    since = now - timedelta(days=days)
    tenders, descriptions = Counter(), defaultdict(Counter)
    for row in rows:
        if not ocds_source.is_goods(row) or not row.get("ocid"):
            continue
        published = ocds_source.parse_date(row.get("published"))
        if published is None or published < since:
            continue
        for code in ocds_source.rubros_of(row):
            tenders[code] += 1
        for item in row.get("items") or []:
            for c in item.get("classifications") or []:
                digits = re.sub(r"\D", "", c.get("id") or "")
                text = (c.get("description") or "").strip()
                # Solo las descripciones UNSPSC (las CUBSO describen un producto concreto, no la familia)
                # (si la clasificación no trae descripción, la API repite la del ítem: no sirve como nombre)
                if ("unspsc" in (c.get("scheme") or "").lower() and len(digits) >= 8 and text
                        and text != (item.get("description") or "").strip()):
                    descriptions[digits[:4]][text[:1].upper() + text[1:].lower()] += 1
    result = []
    for code, count in tenders.most_common():
        examples = [text for text, _ in descriptions[code].most_common(NAME_EXAMPLES)]
        examples = examples[:1] + [e[:1].lower() + e[1:] for e in examples[1:]]
        # "Ej.:" deja claro que no es el nombre oficial de la familia, sino bienes que la componen
        name = PILOT_NAMES.get(code) or (("Ej.: " + ", ".join(examples))[:300] if examples else None)
        result.append((code, name, count))
    return result


def save_catalog(session, entries, now):
    """Reemplaza el catálogo. Si la fuente no trajo nada, se conserva el anterior. Devuelve cuántos guardó."""
    if not entries:
        return 0
    session.query(db.RubroCatalog).delete()
    for code, name, count in entries:
        session.add(db.RubroCatalog(code=code, name=name, tenders_month=count, updated_at=now))
    return len(entries)


def catalog(session):
    """Filas del catálogo, de la familia con más convocatorias a la con menos."""
    return list(session.scalars(select(db.RubroCatalog)
                                .order_by(db.RubroCatalog.tenders_month.desc(), db.RubroCatalog.code)))


def status(session, code):
    """Lo que la pantalla necesita saber de un rubro (HU-013).

    can_request: no tiene convocatorias vigentes guardadas, no es piloto y nadie lo pidió todavía; la
    pantalla ofrece «Solicitar este rubro». pending: ya se pidió, pero aún no corrió la ingesta.
    """
    code = normalize_code(code)
    entry = session.get(db.RubroCatalog, code)
    open_count = open_counts(session).get(code, 0)
    requests = [r for r in session.scalars(select(db.RubroRequest).where(db.RubroRequest.code == code)
                                           .where(db.RubroRequest.active))]
    first_request = min((r.requested_at for r in requests if r.requested_at), default=None)
    # Pendiente: se pidió, pero la última ingesta correcta todavía no lo descargó
    last_ok = session.scalars(select(db.IngestionRun.rubros).where(db.IngestionRun.status == "ok")
                              .order_by(db.IngestionRun.id.desc())).first()
    covered = bool(last_ok) and (last_ok.startswith("todos") or code in last_ok.split(","))
    pending = bool(requests) and not covered
    pilot = code in config.PILOT_RUBROS
    return {"code": code, "name": names(session).get(code), "in_catalog": entry is not None,
            "tenders_month": entry.tenders_month if entry else 0, "open_count": open_count,
            "pilot": pilot, "requested": bool(requests), "requested_at": first_request, "pending": pending,
            "loaded": pilot or bool(requests) or config.ALL_RUBROS,
            "can_request": open_count == 0 and not pilot and not requests}