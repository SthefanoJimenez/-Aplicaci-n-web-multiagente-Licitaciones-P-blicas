"""SP-005 · Factibilidad de la fuente de convocatorias vigentes y bases.

Verifica si la API OCDS del OECE sirve para obtener convocatorias de bienes
vigentes y sus bases. Tiene tres comandos, que se ejecutan en este orden:

  1. discover  Prueba los endpoints de la API y guarda una muestra de cada
               respuesta, para documentar qué entrega y en qué formato.
  2. run       Descarga los archivos mensuales OCDS (mes actual y anterior),
               filtra las convocatorias de bienes vigentes, mide qué campos
               trae cada una, elige los rubros piloto, mide 20 bases
               (enlace, descarga, PDF con texto o escaneado, tokens) y
               escribe el informe.
  3. demo      Lista las convocatorias vigentes de un rubro piloto y
               descarga las bases de una de ellas.

Uso, desde la carpeta analisis/:
    python sp005_01_tender_source.py discover
    python sp005_01_tender_source.py run
    python sp005_01_tender_source.py demo

Si la descarga desde el script falla pero el archivo mensual se puede bajar
desde el navegador, se puede usar:  run --from-file ruta/al/archivo

Salidas en salidas/sp005/. Este script es un spike: no forma parte de la app.
"""
import argparse
import gzip
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
# Las carpetas se pueden cambiar con variables de entorno (las usan las pruebas de punta a punta)
OUTPUT_DIR = Path(os.environ.get("SP005_OUTPUT_DIR") or ROOT / "salidas" / "sp005")
CACHE_DIR = Path(os.environ.get("SP005_CACHE_DIR") or ROOT / "datos" / "cache" / "sp005")
DEFAULT_CATALOG = ROOT.parent / "app" / "data" / "catalog.csv"

BASE_URL = "https://contratacionesabiertas.oece.gob.pe"
# Descarga mensual en JSON, confirmada en el Swagger del portal (/files entrega
# los enlaces csv, csv_es, xlsx, xlsx_es, json y sha de cada mes).
MONTHLY_JSON_PATH = "/api/v1/file/seace_v3/json/{year}/{month}/"
FILES_LIST_PATH = "/api/v1/files?page={page}"
# Endpoints documentados en el portal (Swagger): Release, Record y Files
CANDIDATE_ENDPOINTS = [
    "/api/v1/files?page=1",
    "/api/v1/releases?from=0&size=1",
    "/api/v1/records?from=0&size=1",
    "/api/v1/file/seace_v3/json/{year}/{month}/",
    "/api/v1/file/seace_v3/csv/{year}/{month}/es",
]

LIMA = timezone(timedelta(hours=-5))
LA_LIBERTAD = "LA LIBERTAD"
REQUEST_PAUSE_SECONDS = 0.5
TIMEOUT_SECONDS = 90
# Encabezados como los del Swagger del portal. Se usa User-Agent de navegador
# para evitar bloqueos del WAF de OECE (Oracle Cloud Infrastructure).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"Accept": "*/*", "Referer": BASE_URL + "/api"}

SAMPLE_SIZE = 20            # convocatorias en las que se miden las bases (máximo 2 por entidad)
MIN_OPEN_PER_RUBRO = 5      # vigentes mínimas para que un rubro sea candidato
SCANNED_CHARS_PER_PAGE = 200  # menos de esto en promedio = PDF escaneado (sin capa de texto)
CHARS_PER_TOKEN = 4         # estimación aproximada; ver --count-tokens para el conteo exacto

# Metas propuestas para la conclusión (el equipo puede ajustarlas)
TARGETS = {"fields": 90.0, "link": 80.0, "download": 70.0, "text_pdf": 60.0}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def normalize(text):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/. ]", " ", text)).strip()


def parse_date(value):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return date if date.tzinfo else date.replace(tzinfo=LIMA)


def pct(part, total):
    return round(100 * part / total, 1) if total else 0.0


def session_for(base_url, user_agent=DEFAULT_USER_AGENT):
    session = requests.Session()
    session.headers.update(HEADERS | {"User-Agent": user_agent})
    # Acepta la URL con o sin /api/v1 (igual que sp005_00_run_all.py)
    session.base_url = re.sub(r"/api/v1$", "", base_url.rstrip("/"))
    return session


def months_to_fetch(now, count):
    year, month = now.year, now.month
    months = []
    for _ in range(count):
        months.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return months


# ---------------------------------------------------------------------------
# 1. Descubrimiento de endpoints
# ---------------------------------------------------------------------------
def discover(session, now):
    """Prueba cada endpoint candidato y guarda los primeros 64 KB de la respuesta."""
    folder = OUTPUT_DIR / "discover"
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for i, template in enumerate(CANDIDATE_ENDPOINTS, start=1):
        path = template.format(year=now.year, month=f"{now.month:02d}")
        url = session.base_url + path
        entry = {"url": url}
        try:
            with session.get(url, timeout=TIMEOUT_SECONDS, stream=True) as response:
                head = response.raw.read(65536, decode_content=True)
                entry.update({
                    "status": response.status_code,
                    "content_type": response.headers.get("Content-Type"),
                    "content_length": response.headers.get("Content-Length"),
                    "starts_with": head[:40].decode("latin-1", "replace"),
                })
                (folder / f"{i:02d}_sample.bin").write_bytes(head)
        except requests.RequestException as e:
            entry.update({"status": None, "error": f"{type(e).__name__}: {e}"})
        results.append(entry)
        print(f"{entry.get('status')!s:>5}  {url}")
        time.sleep(REQUEST_PAUSE_SECONDS)
    (OUTPUT_DIR / "discover.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nMuestras en {folder}  ·  resumen en {OUTPUT_DIR / 'discover.json'}")
    return results


# ---------------------------------------------------------------------------
# 2. Descarga y lectura de los archivos OCDS
# ---------------------------------------------------------------------------
def _walk_strings(obj):
    """Todas las cadenas de un JSON (para buscar URLs en la lista de archivos)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_strings(value)


def resolve_monthly_url(session, year, month, max_pages=5):
    """URL del archivo mensual en JSON según /files; si no se encuentra, la ruta por defecto.

    /files responde {"results": [{"source", "year", "month", "files": {"json": url, ...}}, ...]}.
    """
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
        # Respaldo: cualquier URL del listado con la forma .../seace_v3/json/{año}/{mes}
        wanted = re.compile(rf"seace_v3/json/{year}/0?{month}(?!\d)", re.I)
        for text in _walk_strings(listing):
            if wanted.search(text):
                return text if text.startswith("http") else session.base_url + "/" + text.lstrip("/")
        if not results:
            break
    return default


def download_month(session, year, month, refresh=False):
    """Descarga el archivo mensual (o lo toma de la caché). Devuelve su ruta."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / f"ocds_{year}_{month:02d}.bin"
    if target.exists() and not refresh:
        return target
    url = resolve_monthly_url(session, year, month)
    print(f"Descargando {url} …")
    with session.get(url, timeout=TIMEOUT_SECONDS * 4, stream=True) as response:
        response.raise_for_status()
        with open(target, "wb") as file:
            for chunk in response.iter_content(1 << 20):
                file.write(chunk)
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
            pass   # varios objetos seguidos: se lee línea por línea
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("{"):
            yield json.loads(line)


def read_ocds_file(path):
    """Lee un archivo OCDS en ZIP, GZIP o JSON plano y devuelve sus objetos."""
    raw = Path(path).read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for member in archive.namelist():
                if member.lower().endswith((".json", ".jsonl", ".ndjson")):
                    yield from _json_objects(archive.read(member), member.lower())
    elif raw[:2] == b"\x1f\x8b":
        yield from _json_objects(gzip.decompress(raw))
    else:
        yield from _json_objects(raw)


def _merge_releases(releases):
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
                compiled = record.get("compiledRelease") or _merge_releases(record.get("releases", []))
                by_ocid[compiled.get("ocid") or record.get("ocid")].append(compiled)
        elif "releases" in obj:
            for release in obj["releases"]:
                by_ocid[release.get("ocid")].append(release)
        elif "compiledRelease" in obj:
            by_ocid[obj["compiledRelease"].get("ocid")].append(obj["compiledRelease"])
        elif "ocid" in obj:
            by_ocid[obj["ocid"]].append(obj)
    for ocid, releases in by_ocid.items():
        yield releases[0] if len(releases) == 1 else _merge_releases(releases)


# ---------------------------------------------------------------------------
# 3. Normalización de cada convocatoria
# ---------------------------------------------------------------------------
GOODS_VALUES = {"goods", "bien", "bienes"}


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
        if isinstance(value, dict):  # la extensión podría usar {"id": ..., "name": ...}
            value = value.get("name") or value.get("description") or value.get("id")
        if value:
            return str(value).strip().upper(), key
    return None, None


def _classifications(item):
    found = []
    for c in [item.get("classification")] + list(item.get("additionalClassifications") or []):
        if c and c.get("id"):
            found.append({"scheme": str(c.get("scheme", "")), "id": str(c["id"]),
                          "description": c.get("description") or item.get("description")})
    return found


def _valid_deadline(tender):
    """Fecha límite solo si tenderPeriod termina después de empezar.

    En los datos reales del OECE hay convocatorias con tenderPeriod de un solo
    día (inicio = fin = fecha de publicación) aunque siguen "CONVOCADO"; en ese
    caso endDate no es la fecha límite y se trata como dato faltante.
    """
    period = tender.get("tenderPeriod") or {}
    start, end = parse_date(period.get("startDate")), parse_date(period.get("endDate"))
    if end and (start is None or end > start):
        return period.get("endDate")
    return None


def _amount(value):
    """Monto del valor estimado; 0 se trata como no informado (valor reservado o vacío)."""
    amount = (value or {}).get("amount")
    return amount if isinstance(amount, (int, float)) and amount > 0 else None


def normalize_release(release):
    tender = release.get("tender") or {}
    party = _buyer_party(release)
    department, department_key = _department(party)
    items, item_status = [], []
    for item in tender.get("items", []):
        items.append({"description": item.get("description"), "classifications": _classifications(item)})
        item_status.append(str(item.get("statusDetails") or item.get("status") or "").strip())
    documents = [
        {"title": d.get("title"), "type": d.get("documentType"), "format": d.get("format"), "url": d.get("url")}
        for d in (tender.get("documents") or []) + (release.get("documents") or [])
    ]
    status = tender.get("status") or next((s for s in item_status if s), None)
    return {
        "ocid": release.get("ocid"),
        "code": tender.get("id"),
        "title": tender.get("title") or tender.get("description"),
        "description": tender.get("description"),
        "category": tender.get("mainProcurementCategory"),
        "status": status,
        "item_status": " | ".join(sorted({s for s in item_status if s})),
        "method": tender.get("procurementMethodDetails"),
        "published": tender.get("datePublished") or (tender.get("tenderPeriod") or {}).get("startDate") or release.get("date"),
        "deadline": _valid_deadline(tender),
        "tender_period_end": (tender.get("tenderPeriod") or {}).get("endDate"),
        "enquiry_end": (tender.get("enquiryPeriod") or {}).get("endDate"),
        "value": _amount(tender.get("value")),
        "currency": (tender.get("value") or {}).get("currency"),
        "entity": (release.get("buyer") or {}).get("name") or party.get("name"),
        "department": department,
        "department_field": department_key,
        "items": items,
        "documents": documents,
    }


def is_goods(row):
    return normalize(row["category"]) in GOODS_VALUES


OPEN_STATUS = {"active", "convocado"}


def is_open(row, now):
    """Vigente: fecha límite futura si existe; si no, algún ítem "CONVOCADO" (o "active").

    Perfil del 14/10/2026 (854 bienes): tender.status nunca viene, tenderPeriod dura
    un día o falta en el 100% de los casos y el estado real está en
    items[].statusDetails. Por eso, en la práctica, decide el estado de los ítems.
    Limitación: "CONVOCADO" puede incluir procesos cuyo plazo de ofertas ya pasó
    pero aún no tienen buena pro (la fuente no publica esa fecha).
    """
    deadline = parse_date(row["deadline"])
    if deadline is not None:
        return deadline >= now
    statuses = {normalize(row.get("status"))} | {normalize(s) for s in str(row.get("item_status") or "").split("|")}
    return bool(statuses & OPEN_STATUS)


# ---------------------------------------------------------------------------
# 4. Rubros y cobertura del catálogo histórico
# ---------------------------------------------------------------------------
def family_of(item):
    """Rubro = familia UNSPSC (4 primeros dígitos); si no hay UNSPSC, los del primer código."""
    classifications = item["classifications"]
    unspsc = [c for c in classifications if "unspsc" in c["scheme"].lower()]
    chosen = (unspsc or classifications or [{}])[0].get("id", "")
    digits = re.sub(r"\D", "", chosen)
    return digits[:4] if len(digits) >= 4 else None


def load_catalog(path):
    catalog = pd.read_csv(path)
    return {
        "codes": set(catalog["codigoitem"].astype(int)),
        "names": set(catalog["itemcubso"].map(normalize)),
    }


def in_catalog(item, catalog):
    """Coincidencia por código (completo o sus últimos 8 dígitos) o por nombre exacto normalizado."""
    for c in item["classifications"]:
        digits = re.sub(r"\D", "", c["id"])
        if digits and (int(digits) in catalog["codes"] or (len(digits) > 8 and int(digits[-8:]) in catalog["codes"])):
            return "code"
    names = [item["description"]] + [c["description"] for c in item["classifications"]]
    if any(normalize(n) in catalog["names"] for n in names if n):
        return "name"
    return None


def rank_rubros(goods, open_goods, catalog, now):
    """Tabla por rubro: vigentes, en La Libertad, publicadas en 30 días y cobertura del catálogo."""
    stats = defaultdict(lambda: {"open": 0, "open_la_libertad": 0, "published_30d": 0,
                                 "items": 0, "items_in_catalog": 0, "descriptions": Counter()})
    month_ago = now - timedelta(days=30)
    open_ids = {r["ocid"] for r in open_goods}
    for row in goods:
        families = {family_of(i) for i in row["items"]} - {None}
        published = parse_date(row["published"])
        for family in families:
            s = stats[family]
            if published and published >= month_ago:
                s["published_30d"] += 1
            if row["ocid"] in open_ids:
                s["open"] += 1
                s["open_la_libertad"] += row["department"] == LA_LIBERTAD
                for item in row["items"]:
                    if family_of(item) == family:
                        s["items"] += 1
                        s["items_in_catalog"] += in_catalog(item, catalog) is not None
                        s["descriptions"][str(item["description"])[:60]] += 1
    table = pd.DataFrame([
        {"rubro": f, "ejemplos": " | ".join(d for d, _ in s["descriptions"].most_common(3)),
         "vigentes": s["open"], "vigentes_la_libertad": s["open_la_libertad"],
         "publicadas_30_dias": s["published_30d"], "items_vigentes": s["items"],
         "items_en_catalogo": s["items_in_catalog"], "cobertura_catalogo_pct": pct(s["items_in_catalog"], s["items"])}
        for f, s in stats.items()
    ])
    if table.empty:
        return table
    return table.sort_values(["vigentes", "cobertura_catalogo_pct"], ascending=False).reset_index(drop=True)


MIN_CATALOG_COVERAGE = 80.0


def choose_rubros(table, forced=None):
    """Hasta 2 rubros: cobertura del catálogo de 80% o más, 5 o más vigentes y al menos 1 en
    La Libertad; entre ellos, los de más vigentes (volumen real de oportunidades)."""
    if forced:
        return forced
    enough = table[(table["vigentes"] >= MIN_OPEN_PER_RUBRO) & (table["cobertura_catalogo_pct"] >= MIN_CATALOG_COVERAGE)]
    candidates = enough[enough["vigentes_la_libertad"] >= 1]
    if candidates.empty:
        candidates = enough if not enough.empty else table[table["vigentes"] >= MIN_OPEN_PER_RUBRO]
    candidates = candidates.sort_values(["vigentes", "cobertura_catalogo_pct"], ascending=False)
    return candidates["rubro"].head(2).tolist()


# ---------------------------------------------------------------------------
# 5. Bases: enlace, descarga, texto o escaneado, tokens
# ---------------------------------------------------------------------------
BASES_PATTERN = re.compile(r"\bbases?\b|biddingdocuments", re.I)
REQUIREMENTS_PATTERN = re.compile(r"requisitos\s+de\s+calificaci", re.I)
NEXT_SECTION_PATTERN = re.compile(r"\n\s*(cap[ií]tulo|anexo|secci[oó]n)\s", re.I)


def _bases_priority(document):
    """Menor es mejor: bases integradas, luego administrativas, luego otras bases."""
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
    return min(with_url, key=_bases_priority) if with_url else None


GEMINI_TOKENS_PER_PDF_PAGE = 258  # documentación de Gemini (document processing), consultada el 14/10/2026


def _requirements_section(text):
    match = REQUIREMENTS_PATTERN.search(text)
    if not match:
        return None
    rest = text[match.start():]
    end = NEXT_SECTION_PATTERN.search(rest, 200)
    return rest[: end.start()] if end else rest[:20000]


def analyze_pdf(content):
    reader = PdfReader(io.BytesIO(content))
    pages = len(reader.pages)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    chars_per_page = len(text.strip()) / pages if pages else 0
    section = _requirements_section(text)
    match = section is not None
    section = section or ""
    return {
        "kind": "pdf",
        "pages": pages,
        "text_chars": len(text),
        "chars_per_page": round(chars_per_page),
        "has_text": chars_per_page >= SCANNED_CHARS_PER_PAGE,
        "requirements_found": bool(match),
        "requirements_chars": len(section),
        "tokens_full_est": round(len(text) / CHARS_PER_TOKEN),
        "tokens_requirements_est": round(len(section) / CHARS_PER_TOKEN),
        "requirements_text": section,
    }


def analyze_docx(content):
    """Texto de un Word (.docx) leyendo word/document.xml, sin librerías extra."""
    import html
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    section = _requirements_section(text)
    return {
        "kind": "docx",
        "pages": 0,  # Word no guarda el número de páginas de forma fiable
        "text_chars": len(text),
        "chars_per_page": None,
        "has_text": len(text.strip()) >= SCANNED_CHARS_PER_PAGE,
        "requirements_found": section is not None,
        "requirements_chars": len(section or ""),
        "tokens_full_est": round(len(text) / CHARS_PER_TOKEN),
        "tokens_requirements_est": round(len(section or "") / CHARS_PER_TOKEN),
        "requirements_text": section or "",
    }


def file_kind(content):
    """Formato real del archivo según sus primeros bytes (un .docx es un ZIP con word/document.xml)."""
    if content[:4] == b"%PDF":
        return "pdf"
    if content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if "word/document.xml" in archive.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            pass
        return "zip"
    if content[:4] == b"Rar!":
        return "rar"
    if content[:3] == b"7z\xbc":
        return "7z"
    return "otro"


READABLE_SUFFIXES = (".pdf", ".docx")


def pdfs_inside(content, kind):
    """Documentos legibles (PDF o Word) dentro de lo descargado: [(nombre, bytes)]."""
    if kind == "pdf":
        return [("documento.pdf", content)]
    if kind == "docx":
        return [("documento.docx", content)]
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return [(n, archive.read(n)) for n in archive.namelist() if n.lower().endswith(READABLE_SUFFIXES)]
    if kind in ("rar", "7z"):
        # En macOS, "tar" (bsdtar) abre RAR y 7z; en otros sistemas puede no estar disponible
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / f"archivo.{kind}"
            source.write_bytes(content)
            result = subprocess.run(["tar", "-xf", str(source), "-C", tmp], capture_output=True, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(f"no se pudo extraer el {kind} con tar")
            return [(p.name, p.read_bytes()) for p in Path(tmp).rglob("*") if p.suffix.lower() in READABLE_SUFFIXES]
    return []


def analyze_pdfs(documents):
    """Resume uno o varios documentos (PDF o Word): páginas, texto, escaneado, requisitos y tokens."""
    infos, failures = [], []
    for name, content in documents:
        try:
            infos.append(analyze_docx(content) if name.lower().endswith(".docx") else analyze_pdf(content))
        except Exception as error:  # un anexo dañado no invalida el resto
            failures.append(f"{name}: {type(error).__name__}")
    if not infos:
        raise RuntimeError("ningún documento se pudo leer (" + "; ".join(failures) + ")")
    pdf_infos = [i for i in infos if i["kind"] == "pdf"]
    pages = sum(i["pages"] for i in pdf_infos)
    pdf_chars = sum(i["text_chars"] for i in pdf_infos)
    chars = sum(i["text_chars"] for i in infos)
    requirements = max(infos, key=lambda i: i["requirements_chars"]) if infos else {}
    pdf_has_text = bool(pages) and pdf_chars / pages >= SCANNED_CHARS_PER_PAGE
    return {
        "pdfs": len(infos),
        "formato_documento": "+".join(sorted({i["kind"] for i in infos})),
        "pages": pages,
        "text_chars": chars,
        "chars_per_page": round(pdf_chars / pages) if pages else None,
        "has_text": pdf_has_text or any(i["has_text"] for i in infos if i["kind"] == "docx"),
        "tokens_gemini_pdf_est": pages * GEMINI_TOKENS_PER_PDF_PAGE,
        "requirements_found": any(i["requirements_found"] for i in infos),
        "requirements_chars": requirements.get("requirements_chars", 0),
        "tokens_full_est": round(chars / CHARS_PER_TOKEN),
        "tokens_requirements_est": requirements.get("tokens_requirements_est", 0),
        "requirements_text": requirements.get("requirements_text", ""),
    }


def measure_bases(session, sample, folder, count_tokens=None):
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for row in sample:
        doc = bases_document(row)
        result = {"ocid": row["ocid"], "codigo": row["code"], "entidad": row["entity"],
                  "departamento": row["department"], "fecha_limite": row["deadline"],
                  "tiene_enlace": doc is not None, "documento": (doc or {}).get("title"),
                  "tipo": (doc or {}).get("type"), "formato_declarado": (doc or {}).get("format"),
                  "url": (doc or {}).get("url"), "descargable": False, "pdf_obtenido": False, "error": ""}
        if doc:
            try:
                response = session.get(doc["url"], timeout=TIMEOUT_SECONDS)
                content = response.content
                result["http"] = response.status_code
                result["bytes"] = len(content)
                kind = file_kind(content)
                result["formato_real"] = kind
                if response.status_code != 200:
                    result["error"] = f"HTTP {response.status_code}"
                elif kind == "otro":
                    result["error"] = f"Formato no reconocido (empieza con {content[:8]!r})"
                else:
                    result["descargable"] = True
                    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(row["ocid"]))
                    (folder / f"{safe}.{kind}").write_bytes(content)
                    pdfs = pdfs_inside(content, kind)
                    if not pdfs:
                        result["error"] = f"El {kind} no contiene PDF ni Word"
                    else:
                        result["pdf_obtenido"] = True
                        info = analyze_pdfs(pdfs)
                        if count_tokens and info["requirements_text"]:
                            info["tokens_requirements_exact"] = count_tokens(info["requirements_text"])
                        result.update({k: v for k, v in info.items() if k != "requirements_text"})
                        if info["pages"]:
                            result["mb_por_pagina"] = round(len(content) / info["pages"] / 1e6, 2)
            except requests.exceptions.SSLError as e:
                result["error"] = f"SSL: {e}"[:200]
            except requests.RequestException as e:
                result["error"] = f"{type(e).__name__}: {e}"[:200]
            except Exception as e:  # archivo dañado, protegido o no extraíble
                result["error"] = f"No se pudo leer: {type(e).__name__}: {e}"[:200]
            time.sleep(REQUEST_PAUSE_SECONDS)
        rows.append(result)
        if result["pdf_obtenido"]:
            status = f"{result.get('formato_real')} → {result.get('formato_documento')} · " + \
                     ("con texto" if result.get("has_text") else "escaneado")
        else:
            status = result["error"] or "sin enlace"
        print(f"  {row['ocid']}: {status}")
    return pd.DataFrame(rows)


def gemini_token_counter():
    """Conteo exacto con la API de Gemini (opcional; requiere GEMINI_API_KEY)."""
    import os
    from dotenv import load_dotenv
    from google import genai
    load_dotenv(ROOT.parent / "app" / ".env")
    load_dotenv()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    return lambda text: client.models.count_tokens(model=model, contents=text).total_tokens


# ---------------------------------------------------------------------------
# 6. Informe
# ---------------------------------------------------------------------------
# Esenciales: sin ellos no se puede listar ni filtrar la convocatoria (deciden la conclusión).
# Complementarios: se muestran si vienen; si faltan, se reportan como limitación de la fuente.
ESSENTIAL_FIELDS = [("código", "code"), ("entidad", "entity"), ("departamento", "department"),
                    ("estado", "status"), ("fecha de publicación", "published")]
COMPLEMENTARY_FIELDS = [("valor estimado", "value"), ("fecha límite de ofertas", "deadline"),
                        ("fin de consultas", "enquiry_end")]
FIELDS = ESSENTIAL_FIELDS + COMPLEMENTARY_FIELDS


def field_coverage(rows):
    essential = {key for _, key in ESSENTIAL_FIELDS}
    return [{"campo": label, "tipo": "esencial" if key in essential else "complementario",
             "presente": sum(1 for r in rows if r.get(key) not in (None, "")),
             "total": len(rows), "pct": pct(sum(1 for r in rows if r.get(key) not in (None, "")), len(rows))}
            for label, key in FIELDS]


def age_profile(rows, now):
    """Días desde la publicación de las vigentes: muchas antiguas sugieren procesos ya en evaluación."""
    days = [(now - d).days for d in (parse_date(r["published"]) for r in rows) if d]
    return {"con_fecha": len(days), "hasta_7": sum(d <= 7 for d in days),
            "8_a_30": sum(7 < d <= 30 for d in days), "mas_de_30": sum(d > 30 for d in days)}


def conclusion(fields, docs):
    """Conclusión con las metas propuestas en TARGETS."""
    min_field = min((f["pct"] for f in fields if f.get("tipo", "esencial") == "esencial"), default=0)
    n = len(docs)
    link = pct(int(docs["tiene_enlace"].sum()), n) if n else 0
    download = pct(int(docs["descargable"].sum()), n) if n else 0
    obtained = docs[docs["pdf_obtenido"]] if n else docs
    text_pdf = pct(int(obtained["has_text"].fillna(False).astype(bool).sum()), len(obtained)) if len(obtained) else 0
    checks = {"fields": min_field, "link": link, "download": download, "text_pdf": text_pdf}
    met = {k: checks[k] >= TARGETS[k] for k in checks}
    complementary_ok = all(f["pct"] >= TARGETS["fields"] for f in fields if f.get("tipo") == "complementario")
    if all(met.values()) and complementary_ok:
        verdict = "VIABLE"
    elif met["fields"] and link >= 50 and download >= 50:
        verdict = "VIABLE CON RESTRICCIONES"
    else:
        verdict = "NO VIABLE con esta fuente"
    return verdict, checks, met


def _counts(frame, column):
    """Conteo legible de una columna: "pdf 9, zip 1"."""
    if column not in frame or frame.empty:
        return "ninguno"
    return ", ".join(f"{k} {int(v)}" for k, v in frame[column].value_counts().items())


def write_report(context):
    c = context
    lines = [
        "# SP-005 · Informe de factibilidad de la fuente de convocatorias vigentes y bases", "",
        f"Fecha de ejecución: {c['now']:%Y-%m-%d %H:%M} (hora de Lima)  ",
        f"Fuente: {c['base_url']}  ",
        f"Archivos procesados: {', '.join(c['sources'])}", "",
        f"## Conclusión: {c['verdict']}", "",
        "| Medida | Resultado | Meta propuesta | ¿Cumple? |", "|---|---|---|---|",
        f"| Campo esencial menos presente en vigentes de bienes | {c['checks']['fields']}% | {TARGETS['fields']}% | {'Sí' if c['met']['fields'] else 'No'} |",
        f"| Convocatorias con enlace a bases | {c['checks']['link']}% | {TARGETS['link']}% | {'Sí' if c['met']['link'] else 'No'} |",
        f"| Bases descargables | {c['checks']['download']}% | {TARGETS['download']}% | {'Sí' if c['met']['download'] else 'No'} |",
        f"| Documentos con texto (entre los obtenidos) | {c['checks']['text_pdf']}% | {TARGETS['text_pdf']}% | {'Sí' if c['met']['text_pdf'] else 'No'} |", "",
        "Las metas son una propuesta del equipo y pueden ajustarse antes de cerrar la conclusión.", "",
        "## 1. Qué entrega la API", "",
        f"Procesos leídos: {c['n_all']} · de bienes: {c['n_goods']} · de bienes vigentes: {c['n_open']}.", "",
        "| Campo | Tipo | Presente | Total | % |", "|---|---|---|---|---|",
    ]
    lines += [f"| {f['campo']} | {f['tipo']} | {f['presente']} | {f['total']} | {f['pct']}% |" for f in c["fields"]]
    missing = [f["campo"] for f in c["fields"] if f["tipo"] == "complementario" and f["pct"] < TARGETS["fields"]]
    if missing:
        lines += ["", f"Campos complementarios por debajo de la meta (limitación de la fuente): {', '.join(missing)}."]
    a = c["ages"]
    lines += ["", f"Antigüedad de las vigentes (días desde la publicación): hasta 7 días {a['hasta_7']}, "
              f"de 8 a 30 días {a['8_a_30']}, más de 30 días {a['mas_de_30']} (de {a['con_fecha']} con fecha).",
              f"Vigentes de bienes en La Libertad: {c['n_open_ll']}."]
    lines += ["", f"Campo del que se tomó el departamento: {c['department_fields']}."]
    if "region" in c["department_fields"]:
        lines += ["", "Atención: el departamento salió del campo `region`. Revise en vigentes.csv que los valores "
                  "sean departamentos (por ejemplo LA LIBERTAD) y no provincias; en los CSV del OECE se reportó que "
                  "\"Región\" mezcla provincias."]
    lines += ["",
              "## 2. Rubros piloto", "",
              f"Regla: rubros (familia UNSPSC) con cobertura del catálogo histórico de {MIN_CATALOG_COVERAGE:.0f}% o más, "
              f"{MIN_OPEN_PER_RUBRO} o más vigentes y al menos una en La Libertad; entre ellos, los de más vigentes.", "",
              f"Rubros elegidos: **{', '.join(c['rubros']) or 'ninguno'}**", "",
              "| Rubro | Ejemplos | Vigentes | En La Libertad | Publicadas 30 días | Cobertura catálogo |",
              "|---|---|---|---|---|---|"]
    for _, r in c["rubro_table"].head(15).iterrows():
        mark = " ✔" if r["rubro"] in c["rubros"] else ""
        lines.append(f"| {r['rubro']}{mark} | {r['ejemplos']} | {r['vigentes']} | {r['vigentes_la_libertad']} | "
                     f"{r['publicadas_30_dias']} | {r['cobertura_catalogo_pct']}% |")
    docs = c["docs"]
    lines += ["", f"Coincidencias con el catálogo: por código {c['match_counts'].get('code', 0)}, "
              f"por nombre {c['match_counts'].get('name', 0)}, sin coincidencia {c['match_counts'].get(None, 0)}.", "",
              f"## 3. Bases de {len(docs)} convocatorias de los rubros piloto", ""]
    if len(docs):
        lines += [
            f"- Con enlace a bases: {int(docs['tiene_enlace'].sum())} de {len(docs)} ({c['checks']['link']}%).",
            f"- Descargables (PDF, ZIP o RAR): {int(docs['descargable'].sum())} de {len(docs)} ({c['checks']['download']}%).",
            f"- Formatos recibidos: {_counts(docs, 'formato_real')}.",
            f"- Con documento legible (PDF o Word, directo o extraído del comprimido): "
            f"{int(docs['pdf_obtenido'].sum())} de {len(docs)} · tipos: {_counts(docs[docs['pdf_obtenido']], 'formato_documento')}.",
            f"- Con texto: {c['checks']['text_pdf']}% de los documentos obtenidos; el resto parecen escaneados "
            f"(PDF con menos de {SCANNED_CHARS_PER_PAGE} caracteres por página).",
            f"- Entidades distintas en la muestra: {docs['entidad'].nunique()} (máximo {MAX_PER_ENTITY} por entidad).",
        ]
        downloaded = docs[docs["pdf_obtenido"]]
        with_text = downloaded[downloaded["has_text"].fillna(False).astype(bool)] if len(downloaded) else downloaded
        scanned = downloaded[~downloaded["has_text"].fillna(False).astype(bool)] if len(downloaded) else downloaded
        if len(with_text):
            lines += [
                f"- Documentos con texto: tokens estimados del documento completo (mediana) "
                f"{int(with_text['tokens_full_est'].median()):,} ({CHARS_PER_TOKEN} caracteres por token, aproximado).",
                f"- Sección \"requisitos de calificación\" encontrada en {int(with_text['requirements_found'].sum())} "
                f"de {len(with_text)} documentos con texto; tokens estimados de esa sección (mediana): "
                f"{int(with_text.loc[with_text['requirements_found'].astype(bool), 'tokens_requirements_est'].median() if with_text['requirements_found'].any() else 0):,}.",
            ]
        if len(scanned) and "tokens_gemini_pdf_est" in scanned:
            lines += [
                f"- Documentos escaneados: {len(scanned)}; páginas (mediana) {int(scanned['pages'].median())}; "
                f"MB por página (mediana) {scanned['mb_por_pagina'].median() if 'mb_por_pagina' in scanned else '?'}; "
                f"para leerlos con Gemini como PDF se estiman {int(scanned['tokens_gemini_pdf_est'].median()):,} tokens "
                f"(mediana; {GEMINI_TOKENS_PER_PDF_PAGE} tokens por página según la documentación de Gemini).",
            ]
        errors = docs[docs["error"] != ""]["error"].value_counts()
        if len(errors):
            lines += ["", "Errores encontrados:", ""] + [f"- {e}: {n}" for e, n in errors.items()]
    lines += ["", "## 4. Limitaciones de esta medición", "",
              "- Vigente = proceso de bienes con algún ítem en estado CONVOCADO (items[].statusDetails). La fuente "
              "no publica la fecha límite de presentación de ofertas (tenderPeriod dura un día o falta), así que "
              "algunas \"vigentes\" pueden tener el plazo de ofertas vencido y estar en evaluación.",
              "- El valor estimado viene en 0 en la mayoría de convocatorias; se trata como \"no publicado\".",
              "- El departamento es el de la entidad compradora (address.department), no necesariamente el lugar de entrega.",
              "- La fuente se actualiza una vez al día (política de publicación OCDS); no es tiempo real.",
              "- La clasificación texto/escaneado y los tokens son estimaciones; ver el detalle en documentos.csv.",
              "- La muestra de bases es pequeña y empieza por La Libertad; los porcentajes de bases escaneadas "
              "son orientativos, no representativos de todo el país.",
              "", "Detalle: vigentes.csv, rubros.csv, documentos.csv y la carpeta bases/."]
    path = OUTPUT_DIR / "informe_sp005.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------
def load_rows(session, args, now):
    sources, objects = [], []
    if args.from_file:
        paths = [Path(p) for p in args.from_file]
    else:
        paths = [download_month(session, y, m, args.refresh) for y, m in months_to_fetch(now, args.months)]
    for path in paths:
        sources.append(path.name)
        objects.extend(read_ocds_file(path))
    rows = [normalize_release(r) for r in compiled_releases(objects)]
    return rows, sources


MAX_PER_ENTITY = 2


def _spread_sample(rows, size):
    """Muestra en el orden dado, con un máximo de 2 convocatorias por entidad (para no sesgarla)."""
    taken, per_entity = [], Counter()
    for row in rows:
        if per_entity[row["entity"]] < MAX_PER_ENTITY:
            taken.append(row)
            per_entity[row["entity"]] += 1
        if len(taken) == size:
            break
    return taken


def _display_order(row):
    """La Libertad primero y, dentro de cada grupo, lo publicado más recientemente."""
    published = parse_date(row["published"])
    return row["department"] != LA_LIBERTAD, -(published.timestamp() if published else 0)


def command_run(args):
    now = datetime.now(LIMA)
    session = session_for(args.base_url, args.user_agent)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows, sources = load_rows(session, args, now)
    goods = [r for r in rows if is_goods(r)]
    open_goods = [r for r in goods if is_open(r, now)]
    print(f"Procesos: {len(rows)} · bienes: {len(goods)} · bienes vigentes: {len(open_goods)}")

    fields = field_coverage(open_goods)
    catalog = load_catalog(args.catalog)
    match_counts = Counter(in_catalog(i, catalog) for r in open_goods for i in r["items"])
    rubro_table = rank_rubros(goods, open_goods, catalog, now)
    rubros = choose_rubros(rubro_table, args.rubros.split(",") if args.rubros else None) if len(rubro_table) else []

    flat = pd.DataFrame([{k: v for k, v in r.items() if k not in ("items", "documents")} |
                         {"rubros": " ".join(sorted({family_of(i) or "" for i in r["items"]})),
                          "n_documentos": len(r["documents"])} for r in open_goods])
    flat.to_csv(OUTPUT_DIR / "vigentes.csv", index=False, encoding="utf-8-sig")
    rubro_table.to_csv(OUTPUT_DIR / "rubros.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / "vigentes.json").write_text(json.dumps(open_goods, ensure_ascii=False), encoding="utf-8")

    in_pilot = [r for r in open_goods if {family_of(i) for i in r["items"]} & set(rubros)]
    in_pilot.sort(key=_display_order)
    sample = _spread_sample(in_pilot, args.sample)
    print(f"Rubros piloto: {rubros} · convocatorias vigentes en ellos: {len(in_pilot)} · muestra: {len(sample)}")
    counter = gemini_token_counter() if args.count_tokens else None
    docs = measure_bases(session, sample, OUTPUT_DIR / "bases", counter)
    docs.to_csv(OUTPUT_DIR / "documentos.csv", index=False, encoding="utf-8-sig")

    verdict, checks, met = conclusion(fields, docs)
    report = write_report({
        "now": now, "base_url": session.base_url, "sources": sources, "verdict": verdict, "checks": checks,
        "met": met, "n_all": len(rows), "n_goods": len(goods), "n_open": len(open_goods), "fields": fields,
        "ages": age_profile(open_goods, now), "n_open_ll": sum(r["department"] == LA_LIBERTAD for r in open_goods),
        "department_fields": dict(Counter(r["department_field"] for r in open_goods)),
        "rubro_table": rubro_table, "rubros": rubros, "match_counts": match_counts, "docs": docs,
    })
    print(f"\nConclusión preliminar: {verdict}\nInforme: {report}")


def command_demo(args):
    now = datetime.now(LIMA)
    session = session_for(args.base_url, args.user_agent)
    source = OUTPUT_DIR / "vigentes.json"
    if not source.exists():
        sys.exit("Primero ejecute: python sp005_01_tender_source.py run")
    open_goods = [r for r in json.loads(source.read_text(encoding="utf-8")) if is_open(r, now)]
    rubro = args.rubro or pd.read_csv(OUTPUT_DIR / "rubros.csv", dtype=str)["rubro"].iloc[0]
    selected = [r for r in open_goods if rubro in {family_of(i) for i in r["items"]}]
    selected.sort(key=_display_order)
    print(f"\nConvocatorias de bienes vigentes · rubro {rubro} · {len(selected)} encontradas\n")
    for r in selected[:15]:
        value = f"S/ {r['value']:,.2f}" if isinstance(r["value"], (int, float)) else "sin valor"
        published = parse_date(r["published"])
        enquiry = parse_date(r.get("enquiry_end"))
        dates = f"pub. {published:%d/%m}" if published else "pub. ?"
        dates += f" · consultas hasta {enquiry:%d/%m}" if enquiry else ""
        print(f"  {str(r['code'])[:28]:<28} {str(r['department'])[:12]:<12} {dates:<30} {value:>16}  {str(r['title'])[:50]}")
    # Para la demo se busca un PDF con texto (se lee mejor ante el asesor); si en los primeros
    # intentos solo hay PDF escaneados, se guarda el primero de ellos.
    fallback, attempts = None, 0
    for r in selected:
        doc = bases_document(r)
        if not doc or attempts >= MAX_DEMO_ATTEMPTS:
            continue
        attempts += 1
        print(f"\nDescargando bases de {r['code']}: {doc['title']}")
        try:
            response = session.get(doc["url"], timeout=TIMEOUT_SECONDS)
        except requests.RequestException as e:
            print(f"  No se pudo descargar: {type(e).__name__}")
            continue
        if response.status_code != 200:
            print(f"  No se pudo descargar: HTTP {response.status_code}")
            continue
        kind = file_kind(response.content)
        if kind != "pdf":
            print(f"  Las bases vienen en {kind.upper()}; para la demo se busca un PDF. Se prueba con la siguiente.")
            continue
        info = analyze_pdf(response.content)
        if info["has_text"]:
            _save_demo(response.content, info)
            return
        print(f"  PDF escaneado ({info['pages']} páginas); se busca uno con texto.")
        fallback = fallback or (response.content, info)
        time.sleep(REQUEST_PAUSE_SECONDS)
    if fallback:
        _save_demo(*fallback)
        return
    print("\nNinguna convocatoria del rubro tuvo bases en PDF entre las revisadas.")


MAX_DEMO_ATTEMPTS = 6


def _save_demo(content, info):
    target = OUTPUT_DIR / "demo_bases.pdf"
    target.write_bytes(content)
    print(f"Guardado en {target} · {info['pages']} páginas · "
          f"{'con texto' if info['has_text'] else 'escaneado'} · "
          f"requisitos de calificación {'encontrados' if info['requirements_found'] else 'no encontrados'}")


def safe_console():
    """Evita UnicodeEncodeError en consolas que no son UTF-8 (por ejemplo cp1252 en Windows):
    los caracteres que la consola no puede mostrar se reemplazan en lugar de detener el script."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def main():
    safe_console()
    # pypdf avisa de PDF con metadatos mal formados ("Multiple definitions ... /Info"); no afecta la lectura
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=BASE_URL, help="Servidor de la API (para pruebas).")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                        help="Nombre de cliente que se envía a la API (ver la prueba de diagnóstico de la guía).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="Prueba los endpoints y guarda muestras.")
    run = sub.add_parser("run", help="Mide la fuente y escribe el informe.")
    run.add_argument("--months", type=int, default=2, help="Meses a descargar, contando el actual (por defecto 2).")
    run.add_argument("--from-file", nargs="+", help="Usar archivos OCDS ya descargados en lugar de la API.")
    run.add_argument("--rubros", help="Forzar rubros piloto, por ejemplo 4410,3012.")
    run.add_argument("--sample", type=int, default=SAMPLE_SIZE)
    run.add_argument("--catalog", default=DEFAULT_CATALOG, help="Catálogo histórico (app/data/catalog.csv).")
    run.add_argument("--count-tokens", action="store_true", help="Contar tokens exactos con Gemini (usa la API).")
    run.add_argument("--refresh", action="store_true", help="Volver a descargar aunque esté en caché.")
    demo = sub.add_parser("demo", help="Lista vigentes de un rubro y descarga una base.")
    demo.add_argument("--rubro", help="Rubro a mostrar (por defecto, el primero de rubros.csv).")
    args = parser.parse_args()

    if args.command == "discover":
        discover(session_for(args.base_url, args.user_agent), datetime.now(LIMA))
    elif args.command == "run":
        command_run(args)
    else:
        command_demo(args)


if __name__ == "__main__":
    main()