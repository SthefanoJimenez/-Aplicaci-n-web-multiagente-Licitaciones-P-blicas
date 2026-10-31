"""Fuente de convocatorias: API OCDS del OECE (EN-007).

Funciones probadas en el SP-005 (analisis/sp005_01_tender_source.py), movidas aquí para
que las use la ingesta diaria: descarga del archivo mensual, lectura de ZIP/GZIP/JSON,
unión de las entregas por ocid, normalización, regla de vigencia, rubro y elección de las
bases. No usan pandas ni escriben archivos de análisis.

Nombre de cliente (User-Agent): por defecto el de un navegador, porque desde el 28/10/2026
el portal responde 403 al nombre "LicitaFacil-SP005" (evidencia en el SP-006). Se cambia
con la variable de entorno OCDS_USER_AGENT. Es una decisión del PO registrada como riesgo.
"""
import gzip
import io
import json
import re
import tempfile
import time
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import config

BASE_URL = "https://contratacionesabiertas.oece.gob.pe"
MONTHLY_JSON_PATH = "/api/v1/file/seace_v3/json/{year}/{month}/"
FILES_LIST_PATH = "/api/v1/files?page={page}"
RECORD_PATH = "/api/v1/record/{ocid}"

LIMA = timezone(timedelta(hours=-5))
HEADERS = {"Accept": "*/*", "Referer": BASE_URL + "/api"}
REQUEST_PAUSE_SECONDS = 0.5
TIMEOUT_SECONDS = 90


class SourceError(Exception):
    """La fuente no respondió o respondió algo que no se puede leer.

    status: código HTTP cuando lo hay (403 rechazo del portal, 404, 5xx…) o None.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def normalize(text):
    """Minúsculas sin tildes ni signos, para comparar textos de la fuente."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/. ]", " ", text)).strip()


def parse_date(value):
    """Fecha ISO de la fuente con zona horaria; sin zona se asume hora de Lima."""
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return date if date.tzinfo else date.replace(tzinfo=LIMA)


def session_for(base_url=BASE_URL, user_agent=None):
    """Sesión HTTP con los encabezados del Swagger del portal y el nombre de cliente configurado."""
    session = requests.Session()
    session.headers.update(HEADERS | {"User-Agent": user_agent or config.OCDS_USER_AGENT})
    # Acepta la dirección con o sin /api/v1, como el SP-005
    session.base_url = re.sub(r"/api/v1$", "", base_url.rstrip("/"))
    return session


def months_to_fetch(now, count):
    """[(año, mes), …] del mes actual hacia atrás."""
    year, month = now.year, now.month
    months = []
    for _ in range(count):
        months.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return months


# ---------------------------------------------------------------------------
# Descarga y lectura del archivo mensual
# ---------------------------------------------------------------------------
def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_strings(value)


def resolve_monthly_url(session, year, month, max_pages=5):
    """Dirección del archivo mensual en JSON según /files; si no se encuentra, la ruta por defecto."""
    default = session.base_url + MONTHLY_JSON_PATH.format(year=year, month=f"{month:02d}")
    for page in range(1, max_pages + 1):
        try:
            response = session.get(session.base_url + FILES_LIST_PATH.format(page=page), timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            listing = response.json()
        except (requests.RequestException, ValueError):
            return default
        results = listing.get("results", []) if isinstance(listing, dict) else []
        for entry in results:
            if (str(entry.get("source")) == "seace_v3" and str(entry.get("year")) == str(year)
                    and int(entry.get("month", 0)) == month and (entry.get("files") or {}).get("json")):
                return entry["files"]["json"]
        wanted = re.compile(rf"seace_v3/json/{year}/0?{month}(?!\d)", re.I)
        for text in _walk_strings(listing):
            if wanted.search(text):
                return text if text.startswith("http") else session.base_url + "/" + text.lstrip("/")
        if not results:
            break
    return default


def download_month(session, year, month, folder):
    """Descarga el archivo mensual en la carpeta indicada y devuelve su ruta.

    Lanza SourceError si el portal no lo entrega (por ejemplo, 403).
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"ocds_{year}_{month:02d}.bin"
    url = resolve_monthly_url(session, year, month)
    try:
        with session.get(url, timeout=TIMEOUT_SECONDS * 4, stream=True) as response:
            if response.status_code != 200:
                raise SourceError(f"El portal respondió {response.status_code} al pedir {url}",
                                  status=response.status_code)
            with open(target, "wb") as file:
                for chunk in response.iter_content(1 << 20):
                    file.write(chunk)
    except requests.RequestException as e:
        raise SourceError(f"No se pudo descargar {url}: {type(e).__name__}") from e
    time.sleep(REQUEST_PAUSE_SECONDS)
    return target


def _json_objects(raw, name=""):
    """Objetos JSON de un archivo: un JSON completo o uno por línea (JSONL)."""
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return
    if not name.endswith((".jsonl", ".ndjson")):
        try:
            yield json.loads(text)
            return
        except json.JSONDecodeError:
            pass
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("{"):
            yield json.loads(line)


def read_ocds_file(path):
    """Lee un archivo OCDS en ZIP, GZIP o JSON plano y devuelve sus objetos."""
    raw = Path(path).read_bytes()
    try:
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for member in archive.namelist():
                    if member.lower().endswith((".json", ".jsonl", ".ndjson")):
                        yield from _json_objects(archive.read(member), member.lower())
        elif raw[:2] == b"\x1f\x8b":
            yield from _json_objects(gzip.decompress(raw))
        else:
            yield from _json_objects(raw)
    except (zipfile.BadZipFile, OSError, json.JSONDecodeError) as e:
        raise SourceError(f"El archivo {Path(path).name} no se pudo leer: {type(e).__name__}") from e


def merge_releases(releases):
    """Une las entregas de un mismo proceso en orden de fecha (lo más reciente gana)."""
    merged = {}
    for release in sorted(releases, key=lambda r: str(r.get("date", ""))):
        for key, value in release.items():
            if value not in (None, "", [], {}):
                merged[key] = value
    return merged


def compiled_releases(objects):
    """Una versión consolidada por proceso (ocid), venga en records o en releases."""
    by_ocid = defaultdict(list)
    for obj in objects:
        if "records" in obj:
            for record in obj["records"]:
                compiled = record.get("compiledRelease") or merge_releases(record.get("releases", []))
                by_ocid[compiled.get("ocid") or record.get("ocid")].append(compiled)
        elif "releases" in obj:
            for release in obj["releases"]:
                by_ocid[release.get("ocid")].append(release)
        elif "compiledRelease" in obj:
            by_ocid[obj["compiledRelease"].get("ocid")].append(obj["compiledRelease"])
        elif "ocid" in obj:
            by_ocid[obj["ocid"]].append(obj)
    for releases in by_ocid.values():
        yield releases[0] if len(releases) == 1 else merge_releases(releases)


# ---------------------------------------------------------------------------
# Normalización y reglas
# ---------------------------------------------------------------------------
GOODS_VALUES = {"goods", "bien", "bienes"}
OPEN_STATUS = {"active", "convocado"}


def _buyer_party(release):
    buyer_id = (release.get("buyer") or {}).get("id")
    for party in release.get("parties", []):
        roles = party.get("roles", [])
        if (buyer_id and party.get("id") == buyer_id) or "buyer" in roles or "procuringEntity" in roles:
            return party
    return {}


def _department(party):
    address = party.get("address") or {}
    for key in ("department", "departamento", "region", "locality"):
        value = address.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("description") or value.get("id")
        if value:
            return str(value).strip().upper()
    return None


def _classifications(item):
    found = []
    for c in [item.get("classification")] + list(item.get("additionalClassifications") or []):
        if c and c.get("id"):
            found.append({"scheme": str(c.get("scheme", "")), "id": str(c["id"]),
                          "description": c.get("description") or item.get("description")})
    return found


def _valid_deadline(tender):
    """Fecha límite solo si tenderPeriod termina después de empezar (en el OECE suele durar un día)."""
    period = tender.get("tenderPeriod") or {}
    start, end = parse_date(period.get("startDate")), parse_date(period.get("endDate"))
    if end and (start is None or end > start):
        return period.get("endDate")
    return None


def _amount(value):
    """Valor estimado; 0 se trata como no publicado."""
    amount = (value or {}).get("amount")
    return amount if isinstance(amount, (int, float)) and amount > 0 else None


def normalize_release(release):
    """La convocatoria en el formato del SP-005 (lo usa la ingesta para llenar la base)."""
    tender = release.get("tender") or {}
    party = _buyer_party(release)
    items, item_status = [], []
    for item in tender.get("items", []):
        items.append({"description": item.get("description"), "classifications": _classifications(item)})
        item_status.append(str(item.get("statusDetails") or item.get("status") or "").strip())
    documents = [
        {"title": d.get("title"), "type": d.get("documentType"), "format": d.get("format"), "url": d.get("url")}
        for d in (tender.get("documents") or []) + (release.get("documents") or [])
    ]
    return {
        "ocid": release.get("ocid"),
        "code": tender.get("id"),
        "title": tender.get("title") or tender.get("description"),
        "description": tender.get("description"),
        "category": tender.get("mainProcurementCategory"),
        "status": tender.get("status") or next((s for s in item_status if s), None),
        "item_status": " | ".join(sorted({s for s in item_status if s})),
        "method": tender.get("procurementMethodDetails"),
        "published": tender.get("datePublished") or (tender.get("tenderPeriod") or {}).get("startDate") or release.get("date"),
        "deadline": _valid_deadline(tender),
        "value": _amount(tender.get("value")),
        "currency": (tender.get("value") or {}).get("currency"),
        "entity": (release.get("buyer") or {}).get("name") or party.get("name"),
        "department": _department(party),
        "items": items,
        "documents": documents,
    }


def is_goods(row):
    return normalize(row["category"]) in GOODS_VALUES


def is_open(row, now):
    """Vigente: fecha límite futura si existe; si no, algún ítem "CONVOCADO" (regla del SP-005)."""
    deadline = parse_date(row["deadline"])
    if deadline is not None:
        return deadline >= now
    statuses = {normalize(row.get("status"))} | {normalize(s) for s in str(row.get("item_status") or "").split("|")}
    return bool(statuses & OPEN_STATUS)


def family_of(item):
    """Rubro = familia UNSPSC (4 primeros dígitos); si no hay UNSPSC, los del primer código."""
    classifications = item["classifications"]
    unspsc = [c for c in classifications if "unspsc" in c["scheme"].lower()]
    chosen = (unspsc or classifications or [{}])[0].get("id", "")
    digits = re.sub(r"\D", "", chosen)
    return digits[:4] if len(digits) >= 4 else None


def rubros_of(row):
    return sorted({family_of(i) for i in row["items"]} - {None})


BASES_PATTERN = re.compile(r"\bbases?\b|biddingdocuments", re.I)


def bases_priority(document):
    """Menor es mejor: bases integradas, luego administrativas, luego otras bases, luego el resto."""
    title = normalize(document.get("title"))
    if "bases integradas" in title:
        return 0
    if "bases administrativas" in title:
        return 1
    if BASES_PATTERN.search(f"{document.get('type')} {document.get('title')}"):
        return 2
    return 3


def bases_document(row):
    """Las bases de la convocatoria (integradas si ya existen), o el primer documento con enlace."""
    with_url = [d for d in row["documents"] if d.get("url")]
    return min(with_url, key=bases_priority) if with_url else None


# ---------------------------------------------------------------------------
# Lectura completa para la ingesta
# ---------------------------------------------------------------------------
def fetch_rows(now, months=None, session=None, folder=None):
    """Descarga los meses configurados y devuelve (convocatorias normalizadas, archivos leídos).

    Los archivos se guardan en una carpeta temporal que se borra al terminar, salvo que se
    indique otra. Lanza SourceError si algún mes no se puede descargar o leer.
    """
    session = session or session_for()
    months = months or config.OCDS_MONTHS
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(folder or tmp)
        paths = [download_month(session, y, m, target) for y, m in months_to_fetch(now, months)]
        objects = []
        for path in paths:
            objects.extend(read_ocds_file(path))
    rows = [normalize_release(r) for r in compiled_releases(objects)]
    return rows, [p.name for p in paths]