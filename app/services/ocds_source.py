"""Fuente de convocatorias: API OCDS del OECE (EN-007).

Funciones probadas en el SP-005 (analisis/sp005_01_tender_source.py), movidas aquí para
que las use la ingesta diaria: descarga del archivo mensual, lectura de ZIP/GZIP/JSON,
unión de las entregas por ocid, normalización, regla de vigencia, rubro y elección de las
bases. No usan pandas ni escriben archivos de análisis.

Nombre de cliente (User-Agent): por defecto el de un navegador, porque desde el 28/10/2026
el portal responde 403 al nombre "LicitaFacil-SP005" (evidencia en el SP-006). Se cambia
con la variable de entorno OCDS_USER_AGENT. Es una decisión del PO registrada como riesgo.

Mes aún no publicado (corrección del 01/11/2026): el 01/11 la ingesta falló con 403 al pedir
el archivo de 2026/11, que el portal todavía no había generado. Ahora se usa el listado
/files como fuente de verdad: un mes que no figura ahí se omite (y se informa) en lugar de
tumbar toda la ejecución. Si el listado no responde, el mes actual que falla con 403/404 se
omite solo si el mes anterior sí se descarga (un bloqueo real afectaría a ambos).
"""
import gzip
import html
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
HEADERS = {"Accept": "*/*", "Accept-Language": "es-PE,es;q=0.9", "Referer": BASE_URL + "/api"}
REQUEST_PAUSE_SECONDS = 0.5
TIMEOUT_SECONDS = 90


class SourceError(Exception):
    """La fuente no respondió o respondió algo que no se puede leer.

    status: código HTTP cuando lo hay (403 rechazo del portal, 404, 5xx…) o None.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def describe_rejection(response):
    """Pistas de quién rechazó la consulta (servidor, tipo y comienzo de la respuesta).

    Sirve para distinguir en el resumen de GitHub Actions un archivo que no existe de un
    bloqueo del firewall del portal. No incluye encabezados de la solicitud ni datos propios.
    """
    server = response.headers.get("Server") or "?"
    kind = (response.headers.get("Content-Type") or "?").split(";")[0]
    try:
        head = next(response.iter_content(300), b"")
    except (requests.RequestException, StopIteration):
        head = b""
    text = html.unescape(re.sub(r"<[^>]+>", " ", head.decode("utf-8", errors="replace")))
    text = re.sub(r"\s+", " ", text).strip()[:160]
    return f"servidor: {server} · tipo: {kind}" + (f" · respuesta: «{text}»" if text else "")


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


def default_monthly_url(session, year, month):
    return session.base_url + MONTHLY_JSON_PATH.format(year=year, month=f"{month:02d}")


def _url_in_listing(session, listing, year, month):
    """Dirección del JSON de ese mes dentro de una página de /files, o None si no figura."""
    results = listing.get("results", []) if isinstance(listing, dict) else []
    for entry in results:
        try:
            same_month = int(entry.get("month", 0)) == month
        except (TypeError, ValueError):
            same_month = False
        if (str(entry.get("source")) == "seace_v3" and str(entry.get("year")) == str(year)
                and same_month and (entry.get("files") or {}).get("json")):
            return entry["files"]["json"]
    wanted = re.compile(rf"seace_v3/json/{year}/0?{month}(?!\d)", re.I)
    for text in _walk_strings(listing):
        if wanted.search(text):
            return text if text.startswith("http") else session.base_url + "/" + text.lstrip("/")
    return None


def monthly_listing(session, months, max_pages=5):
    """Lee /files y devuelve ({(año, mes): url} de los meses pedidos que figuran, nota).

    Si /files no responde o no se puede leer, devuelve (None, nota con el motivo): quien llama
    usa entonces las direcciones por defecto, como antes. La nota sirve para el diagnóstico.
    """
    pending = set(months)
    found = {}
    for page in range(1, max_pages + 1):
        url = session.base_url + FILES_LIST_PATH.format(page=page)
        try:
            with session.get(url, timeout=TIMEOUT_SECONDS, stream=True) as response:
                if response.status_code != 200:
                    return None, f"/files respondió {response.status_code} ({describe_rejection(response)})"
                listing = json.loads(response.content)
        except requests.RequestException as e:
            return None, f"/files no respondió ({type(e).__name__})"
        except ValueError:
            return None, "/files respondió algo que no es JSON"
        for year, month in list(pending):
            found_url = _url_in_listing(session, listing, year, month)
            if found_url:
                found[(year, month)] = found_url
                pending.discard((year, month))
        results = listing.get("results", []) if isinstance(listing, dict) else []
        if not pending or not results:
            break
    return found, "/files respondió 200"


def resolve_monthly_url(session, year, month, max_pages=5):
    """Dirección del archivo mensual en JSON según /files; si no se encuentra, la ruta por defecto."""
    found, _note = monthly_listing(session, [(year, month)], max_pages)
    return (found or {}).get((year, month)) or default_monthly_url(session, year, month)


def download_month(session, year, month, folder, url=None):
    """Descarga el archivo mensual en la carpeta indicada y devuelve su ruta.

    Lanza SourceError si el portal no lo entrega (por ejemplo, 403).
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"ocds_{year}_{month:02d}.bin"
    url = url or resolve_monthly_url(session, year, month)
    try:
        with session.get(url, timeout=TIMEOUT_SECONDS * 4, stream=True) as response:
            if response.status_code != 200:
                raise SourceError(f"El portal respondió {response.status_code} al pedir {url} "
                                  f"({describe_rejection(response)})", status=response.status_code)
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
NOT_PUBLISHED_STATUS = (403, 404)


def fetch_rows(now, months=None, session=None, folder=None):
    """Descarga los meses configurados y devuelve (convocatorias, archivos leídos, meses omitidos).

    meses omitidos: [("2026-11", motivo)] de los meses que el portal todavía no publica.
    Los archivos se guardan en una carpeta temporal que se borra al terminar, salvo que se
    indique otra. Lanza SourceError si no se pudo leer ningún mes, o si falla un mes que no
    es el actual (eso no es "aún no publicado": es un rechazo o una falla del portal).
    """
    session = session or session_for()
    months = months or config.OCDS_MONTHS
    wanted = months_to_fetch(now, months)
    listing, listing_note = monthly_listing(session, wanted)
    skipped, deferred_error = [], None
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(folder or tmp)
        paths = []
        for year, month in wanted:
            label = f"{year}-{month:02d}"
            if listing is not None and (year, month) not in listing:
                skipped.append((label, "el listado /files aún no lo incluye"))
                continue
            url = (listing or {}).get((year, month)) or default_monthly_url(session, year, month)
            try:
                paths.append(download_month(session, year, month, target, url=url))
            except SourceError as error:
                is_current = (year, month) == wanted[0]
                if is_current and error.status in NOT_PUBLISHED_STATUS and len(wanted) > 1:
                    deferred_error = SourceError(f"{error} · {listing_note}", status=error.status)
                    skipped.append((label, f"respondió {error.status}; se asume aún no publicado"))
                    continue
                raise SourceError(f"{error} · {listing_note}", status=error.status) from error
        if not paths:
            if deferred_error:
                raise deferred_error
            raise SourceError(f"El portal no publica ninguno de los meses pedidos ({', '.join(s[0] for s in skipped)})"
                              f" · {listing_note}")
        objects = []
        for path in paths:
            objects.extend(read_ocds_file(path))
    rows = [normalize_release(r) for r in compiled_releases(objects)]
    return rows, [p.name for p in paths], skipped


def diagnose(session=None, now=None):
    """Consultas pequeñas para separar "archivo aún no publicado" de "bloqueo del portal".

    Devuelve [(qué se pidió, código o motivo, detalle)]. No descarga archivos completos: lee
    solo los encabezados y el comienzo de cada respuesta. La usa `python ingest_job.py --diagnostico`.
    """
    session = session or session_for()
    now = now or datetime.now(LIMA)
    results = []

    def probe(label, url):
        try:
            with session.get(url, timeout=TIMEOUT_SECONDS, stream=True) as response:
                detail = (response.headers.get("Content-Type") or "?").split(";")[0]
                if response.status_code != 200:
                    detail = describe_rejection(response)
                results.append((label, response.status_code, detail))
        except requests.RequestException as e:
            results.append((label, "sin respuesta", type(e).__name__))
        time.sleep(REQUEST_PAUSE_SECONDS)

    probe("listado /files (página 1)", session.base_url + FILES_LIST_PATH.format(page=1))
    listing, _note = monthly_listing(session, months_to_fetch(now, 2))
    for year, month in months_to_fetch(now, 2):
        listed = "figura en /files" if listing and (year, month) in listing else (
            "NO figura en /files" if listing is not None else "listado no disponible")
        url = (listing or {}).get((year, month)) or default_monthly_url(session, year, month)
        probe(f"archivo {year}-{month:02d} ({listed})", url)
    return results