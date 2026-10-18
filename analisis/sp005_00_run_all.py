"""SP-005 · Ejecuta todo el spike, de la parte A a la D de la guía, con un solo comando.

  A. Preparación          archivos, dependencias y pruebas sin internet (pytest)
  B. Pruebas de la API    B0 identificación, B1 /files, B2 archivo del mes, B3 /releases,
                          B4 descarga del mes, B5 perfil de campos, B6 muestra,
                          B7 /record, B8 /release, B9 descarga de unas bases
  C. Medición             C1 discover y C2 run (filtro, rubros, bases, informe)
  D. Revisión y demo      D1 revisión de resultados y D2 demo

Uso, desde analisis/ y con el entorno virtual activo:
    python sp005_00_run_all.py                    (todo, con 20 bases)
    python sp005_00_run_all.py --partes A,B       (solo algunas partes)
    python sp005_00_run_all.py --sample 10 --rubros 1510,5013
    python sp005_00_run_all.py --sin-descarga     (reusar lo ya descargado)

Al final escribe salidas/sp005/resumen_a_d.md con el resultado de cada paso.
Las consultas se identifican con un nombre de cliente del proyecto. Si el portal
responde 403 o entrega una página HTML en lugar de un archivo, se anota como
resultado y se sigue: no se reintenta ni se intenta forzar el acceso.
"""
import argparse
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("SP005_OUTPUT_DIR") or ROOT / "salidas" / "sp005")
API_DIR = OUTPUT_DIR / "pruebas_api"
MAIN_SCRIPT = ROOT / "sp005_01_tender_source.py"
PROFILE_SCRIPT = ROOT / "sp005_02_profile_fields.py"
TESTS_DIR = ROOT / "tests_sp005"
DEFAULT_CATALOG = ROOT.parent / "app" / "data" / "catalog.csv"

BASE_URL = "https://contratacionesabiertas.oece.gob.pe"
USER_AGENT = "LicitaFacil-SP005/1.0 (proyecto academico UPAO; consultas de baja frecuencia)"
LIMA = timezone(timedelta(hours=-5))
TIMEOUT = 90
PAUSE = 0.5
REQUIRED_PACKAGES = ["pandas", "requests", "pypdf", "pytest", "reportlab", "PIL"]
GITIGNORE_LINES = ["analisis/salidas/sp005/bases/", "analisis/salidas/sp005/demo_bases.pdf"]

OK, WARN, FAIL, SKIP = "OK", "AVISO", "FALLÓ", "OMITIDO"
results = []   # (paso, estado, detalle)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def say(text=""):
    print(text, flush=True)


def record(step, status, detail=""):
    results.append((step, status, detail))
    say(f"   [{status}] {detail}" if detail else f"   [{status}]")


def header(title):
    say("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def file_kind(content):
    """Formato real según los primeros bytes (un .docx es un ZIP con word/document.xml)."""
    if content[:5] == b"%PDF-":
        return "pdf"
    if content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return "docx" if "word/document.xml" in archive.namelist() else "zip"
        except zipfile.BadZipFile:
            return "zip"
    if content[:4] == b"Rar!":
        return "rar"
    if content[:3] == b"7z\xbc":
        return "7z"
    if content.lstrip()[:1] == b"<":
        return "html"
    return "otro"


def list_archive(content, kind):
    """Nombres dentro de un ZIP, o de un RAR/7z con tar (bsdtar en macOS)."""
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return archive.namelist()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"archivo.{kind}"
        source.write_bytes(content)
        result = subprocess.run(["tar", "-tf", str(source)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "tar no pudo leer el archivo").strip())
    return [name for name in result.stdout.splitlines() if name.strip()]


def first_list(data, key):
    """data[key] si es lista; si la respuesta es un solo objeto, [data]."""
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return data[key]
    return [data] if isinstance(data, dict) else []


def api_root(base_url):
    """Acepta la URL con o sin /api/v1 y devuelve (servidor, servidor/api/v1)."""
    host = re.sub(r"/api/v1$", "", base_url.rstrip("/"))
    return host, host + "/api/v1"


def run_script(args, step, capture=False):
    """Ejecuta un script del spike con el mismo Python; muestra su salida en vivo."""
    command = [sys.executable, *map(str, args)]
    if capture:
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        return result.returncode, result.stdout + result.stderr
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
    lines = []
    for line in process.stdout:
        print("   " + line.rstrip(), flush=True)
        lines.append(line)
    process.wait()
    return process.returncode, "".join(lines)


class Api:
    """Cliente HTTP identificado; guarda solo lo que llega bien para no pisar descargas previas."""

    def __init__(self, api_base, user_agent):
        import requests
        self.requests = requests
        self.base = api_base
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "*/*"})

    def get(self, url, path=None, method="GET"):
        try:
            response = self.session.request(method, url, timeout=TIMEOUT, allow_redirects=True)
        except self.requests.RequestException as error:
            say(f"   Error de conexión: {type(error).__name__}: {error}")
            return None
        saved = path is not None and response.status_code == 200 and response.content
        if saved:
            path.write_bytes(response.content)
        size = response.headers.get("Content-Length") if method == "HEAD" else f"{len(response.content):,}"
        say(f"   HTTP {response.status_code} · {response.headers.get('Content-Type', '?')} · {size} bytes"
            + (f" → {path.name}" if saved else ""))
        time.sleep(PAUSE)
        return response


# ---------------------------------------------------------------------------
# A. Preparación
# ---------------------------------------------------------------------------
def part_a(args):
    header("A. Preparación")
    say("\nA1 · Archivos del spike")
    missing = [p.name for p in (MAIN_SCRIPT, PROFILE_SCRIPT, TESTS_DIR) if not p.exists()]
    record("A1 Archivos", FAIL if missing else OK, f"faltan: {', '.join(missing)}" if missing else "scripts y pruebas presentes")
    catalog = Path(args.catalog)
    record("A1 Catálogo", OK if catalog.exists() else WARN,
           str(catalog) if catalog.exists() else f"no se encontró {catalog}; la cobertura del catálogo saldrá vacía")

    say("\nA2 · .gitignore de la raíz")
    gitignore = ROOT.parent / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    absent = [line for line in GITIGNORE_LINES if line not in text]
    record("A2 .gitignore", WARN if absent else OK,
           f"agregue: {', '.join(absent)}" if absent else "las bases descargadas no se subirán a GitHub")

    say("\nA3 · Dependencias")
    lacking = []
    for name in REQUIRED_PACKAGES:
        try:
            module = importlib.import_module(name)
            say(f"   {name} {getattr(module, '__version__', '')}")
        except ImportError:
            lacking.append(name)
    record("A3 Dependencias", FAIL if lacking else OK,
           f"faltan {', '.join(lacking)}: ejecute pip install -r requirements.txt" if lacking else "todas instaladas")
    if lacking or missing:
        return False

    say("\nA4 · Pruebas sin internet (pytest)")
    code, output = run_script(["-m", "pytest", TESTS_DIR, "-q", "-p", "no:cacheprovider"], "A4", capture=True)
    summary = next((l for l in reversed(output.strip().splitlines()) if "passed" in l or "failed" in l or "error" in l), "")
    if code != 0:
        say(output[-3000:])
    record("A4 pytest", OK if code == 0 else FAIL, summary.strip("= ") or f"código {code}")
    return code == 0


# ---------------------------------------------------------------------------
# B. Pruebas de la API
# ---------------------------------------------------------------------------
def part_b(args, now):
    header("B. Pruebas de la API")
    import requests
    API_DIR.mkdir(parents=True, exist_ok=True)
    _, api_base = api_root(args.base_url)
    api = Api(api_base, args.user_agent)
    year, month = args.anio, args.mes

    say("\nB0 · Identificación del cliente (una consulta sin nombre propio y otra con él)")
    try:
        anonymous = requests.get(f"{api_base}/files?page=1", timeout=TIMEOUT)
        say(f"   Sin identificar: HTTP {anonymous.status_code}")
    except requests.RequestException as error:
        anonymous = None
        say(f"   Sin identificar: error de conexión ({type(error).__name__})")
    identified = api.get(f"{api_base}/files?page=1", API_DIR / "01_files.json")
    if identified is None:
        record("B0 Identificación", FAIL, "sin conexión con la API; revise internet")
        return False
    record("B0 Identificación", OK if identified.status_code == 200 else FAIL,
           f"sin identificar {getattr(anonymous, 'status_code', '?')} · identificado {identified.status_code}")

    say("\nB1 · Lista de archivos masivos · GET /files")
    try:
        listing = identified.json().get("results", [])
        current = next((e for e in listing if str(e.get("year")) == str(year) and int(e.get("month", 0)) == month), None)
        record("B1 /files", OK if current else WARN,
               f"{len(listing)} mes(es) en la página 1; mes {year}-{month:02d} actualizado {current.get('timestamp') if current else 'no listado'}")
    except (ValueError, AttributeError):
        record("B1 /files", FAIL, "la respuesta no es el JSON esperado")

    say("\nB2 · Archivo del mes (solo encabezados) · HEAD /file/seace_v3/json/{año}/{mes}/")
    month_url = f"{api_base}/file/seace_v3/json/{year}/{month:02d}/"
    head = api.get(month_url, method="HEAD")
    if head is not None:
        record("B2 Archivo del mes", OK if head.status_code == 200 else WARN,
               f"{head.headers.get('Content-Type')} · {head.headers.get('Content-Length')} bytes · "
               f"modificado {head.headers.get('Last-Modified', '?')}")

    say("\nB3 · Búsqueda de releases · GET /releases?from=0&size=2")
    releases = api.get(f"{api_base}/releases?from=0&size=2", API_DIR / "03_releases.json")
    if releases is not None and releases.ok:
        try:
            first = first_list(releases.json(), "releases")[0]
            tender = first.get("tender") or {}
            period = tender.get("tenderPeriod") or {}
            say(f"   ocid {first.get('ocid')} · tender.id {tender.get('id')} · {tender.get('mainProcurementCategory')}")
            say(f"   tenderPeriod {period.get('startDate')} → {period.get('endDate')} · valor {(tender.get('value') or {}).get('amount')}")
            say(f"   estados de ítems {[i.get('statusDetails') for i in tender.get('items', [])]}")
            say(f"   documentos {[(d.get('documentType'), d.get('format')) for d in tender.get('documents') or []]}")
            record("B3 /releases", OK, "estructura OCDS leída")
        except (ValueError, IndexError):
            record("B3 /releases", WARN, "la respuesta no trae releases")
    else:
        record("B3 /releases", WARN, "sin respuesta 200")

    say(f"\nB4 · Descarga del archivo del mes {year}-{month:02d}")
    zip_path = API_DIR / f"04_{year}_{month:02d}.zip"
    if args.sin_descarga and zip_path.exists():
        say(f"   Se reusa {zip_path.name}")
    else:
        response = api.get(month_url, zip_path)
        if response is None or response.status_code != 200 or file_kind(response.content) != "zip":
            record("B4 Descarga del mes", FAIL, "no se obtuvo el ZIP; los pasos B5 a B9 dependen de él")
            return False
    with zipfile.ZipFile(zip_path) as archive:
        record("B4 Descarga del mes", OK, f"{zip_path.stat().st_size:,} bytes · contiene {', '.join(archive.namelist())}")

    say("\nB5 · Perfil de campos")
    code, output = run_script([PROFILE_SCRIPT, zip_path], "B5", capture=True)
    if code != 0:
        say(output[-2000:])
        record("B5 Perfil", FAIL, "el perfil falló")
        return False
    lines = output.splitlines()
    record("B5 Perfil", OK, (lines[1] if len(lines) > 1 else "") + " · ver perfil_campos.txt")

    say("\nB6 · Convocatoria de la muestra")
    sample = json.loads((OUTPUT_DIR / "muestra_bienes.json").read_text(encoding="utf-8"))
    if not sample:
        record("B6 Muestra", FAIL, "el archivo no trae bienes")
        return False
    release = sample[min(args.muestra, len(sample) - 1)]
    tender = release.get("tender") or {}
    ocid, tender_id = release.get("ocid"), tender.get("id")
    bases = [d for d in tender.get("documents") or [] if d.get("documentType") == "biddingDocuments" and d.get("url")]
    say(f"   OCID {ocid} · tender.id {tender_id} · {tender.get('title')} · {(release.get('buyer') or {}).get('name')}")
    for item in tender.get("items", [])[:5]:
        say(f"   Ítem: {item.get('statusDetails')} | {str(item.get('description', ''))[:70]}")
    record("B6 Muestra", OK, f"{ocid} · {len(bases)} documento(s) de bases")

    say("\nB7 · Proceso completo · GET /record/{ocid}")
    response = api.get(f"{api_base}/record/{ocid}", API_DIR / "07_record.json")
    try:
        rec = first_list(response.json(), "records")[0] if response is not None and response.ok else None
    except (ValueError, IndexError):
        rec = None
    record("B7 /record", OK if rec else WARN,
           f"{len(rec.get('releases', []))} versión(es) · compiledRelease {'sí' if 'compiledRelease' in rec else 'no'}"
           if rec else "sin respuesta válida")

    say("\nB8 · Versiones del proceso · GET /release/seace_v3/{tender.id}")
    response = api.get(f"{api_base}/release/seace_v3/{tender_id}", API_DIR / "08_release_tender.json")
    try:
        versions = first_list(response.json(), "releases") if response is not None and response.ok else []
    except ValueError:
        versions = []
    for version in versions:
        say(f"   {version.get('date')} | {version.get('tag')} | "
            f"{[i.get('statusDetails') for i in (version.get('tender') or {}).get('items', [])]}")
    record("B8 /release", OK if versions else WARN, f"{len(versions)} versión(es)")

    say("\nB9 · Descarga de las bases")
    if not bases:
        record("B9 Bases", WARN, "esta convocatoria no publicó bases; pruebe --muestra 1")
        return True
    say(f"   Formato declarado: {bases[0].get('format')} · {bases[0]['url']}")
    response = api.get(bases[0]["url"], API_DIR / "09_bases.bin")
    if response is None or response.status_code != 200:
        status = getattr(response, "status_code", "sin conexión")
        record("B9 Bases", WARN, f"HTTP {status}: se anota como resultado; no se fuerza")
        return True
    kind = file_kind(response.content)
    detail = f"formato real {kind}, {len(response.content):,} bytes"
    if kind in ("zip", "rar", "7z"):
        try:
            names = list_archive(response.content, kind)
            detail += f" · contiene {len(names)} archivo(s), {sum(n.lower().endswith(('.pdf', '.docx')) for n in names)} PDF o Word"
        except (RuntimeError, zipfile.BadZipFile) as error:
            detail += f" · no se pudo listar: {error}"
    record("B9 Bases", WARN if kind in ("html", "otro") else OK, detail)
    return True


# ---------------------------------------------------------------------------
# C. Medición
# ---------------------------------------------------------------------------
def part_c(args):
    header("C. Medición con el script principal")
    common = [MAIN_SCRIPT, "--base-url", args.base_url, "--user-agent", args.user_agent]

    say("\nC1 · discover")
    code, output = run_script(common + ["discover"], "C1")
    codes = re.findall(r"^\s*(\d{3}|None)\s+http", output, re.M)
    record("C1 discover", OK if code == 0 and "200" in codes else WARN, f"códigos {', '.join(codes) or '?'}")

    say(f"\nC2 · run (muestra de {args.sample} bases)")
    run_args = common + ["run", "--sample", str(args.sample), "--catalog", args.catalog]
    if args.rubros:
        run_args += ["--rubros", args.rubros]
    if not args.sin_descarga:
        run_args += ["--refresh"]
    code, output = run_script(run_args, "C2")
    verdict = re.search(r"Conclusión preliminar: (.+)", output)
    record("C2 run", OK if code == 0 else FAIL,
           f"conclusión: {verdict.group(1)}" if verdict else f"terminó con código {code}")
    return code == 0


# ---------------------------------------------------------------------------
# D. Revisión y demo
# ---------------------------------------------------------------------------
def part_d(args):
    header("D. Revisión y demo")
    say("\nD1 · Revisión de resultados")
    report = OUTPUT_DIR / "informe_sp005.md"
    if not report.exists():
        record("D1 Revisión", FAIL, "no hay informe; ejecute antes la parte C")
        return False
    text = report.read_text(encoding="utf-8")
    department = re.search(r"Campo del que se tomó el departamento: (.+)\.", text)
    rubros = re.search(r"Rubros elegidos: \*\*(.+?)\*\*", text)
    unmet = re.findall(r"^\| (.+?) \| [^|]+ \| [^|]+ \| No \|$", text, re.M)
    record("D1 Departamento", WARN if department and "region" in department.group(1) else OK,
           department.group(1) if department else "no informado")
    record("D1 Rubros", OK if rubros and rubros.group(1) != "ninguno" else WARN,
           f"{rubros.group(1) if rubros else '?'}; confirme con el equipo que tienen sentido para el negocio")
    record("D1 Metas", WARN if unmet else OK, f"no cumplen: {'; '.join(unmet)}" if unmet else "las cuatro metas se cumplen")

    say("\nD2 · demo")
    code, output = run_script([MAIN_SCRIPT, "--base-url", args.base_url, "--user-agent", args.user_agent, "demo"], "D2")
    saved = "Guardado en" in output
    record("D2 demo", OK if code == 0 and saved else WARN,
           "bases de una convocatoria guardadas en demo_bases.pdf" if saved else "ninguna convocatoria del rubro tuvo bases en PDF")
    return True


# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------
def write_summary(now, args):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# SP-005 · Resumen de la ejecución A–D", "",
             f"Fecha: {now:%Y-%m-%d %H:%M} (hora de Lima) · Fuente: {args.base_url} · Partes: {args.partes}", "",
             "| Paso | Estado | Detalle |", "|---|---|---|"]
    lines += [f"| {step} | {status} | {detail.replace('|', '/')} |" for step, status, detail in results]
    outputs = sorted(p.relative_to(OUTPUT_DIR).as_posix() for p in OUTPUT_DIR.rglob("*")
                     if p.is_file() and "bases" not in p.parts and "discover" not in p.parts)
    lines += ["", "Archivos generados en salidas/sp005/:", ""] + [f"- {name}" for name in outputs]
    path = OUTPUT_DIR / "resumen_a_d.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    now = datetime.now(LIMA)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--partes", default="A,B,C,D", help="Partes a ejecutar, por ejemplo A,B (por defecto todas).")
    parser.add_argument("--sample", type=int, default=20, help="Bases a medir en C2 (por defecto 20).")
    parser.add_argument("--rubros", help="Forzar rubros piloto en C2, por ejemplo 1510,5013.")
    parser.add_argument("--muestra", type=int, default=0, help="Convocatoria de la muestra usada en B6–B9 (0, 1 o 2).")
    parser.add_argument("--sin-descarga", action="store_true", help="Reusar el ZIP y la caché ya descargados.")
    parser.add_argument("--continuar", action="store_true", help="Seguir con la parte siguiente aunque una falle.")
    parser.add_argument("--anio", type=int, default=now.year)
    parser.add_argument("--mes", type=int, default=now.month)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="Catálogo histórico (app/data/catalog.csv).")
    parser.add_argument("--base-url", default=BASE_URL, help="Servidor de la API (con o sin /api/v1).")
    parser.add_argument("--user-agent", default=USER_AGENT, help="Nombre de cliente que se envía a la API.")
    args = parser.parse_args()
    parts = [p.strip().upper() for p in args.partes.split(",") if p.strip()]

    say(f"SP-005 · ejecución {', '.join(parts)} · {now:%Y-%m-%d %H:%M} (hora de Lima)")
    steps = {"A": lambda: part_a(args), "B": lambda: part_b(args, now), "C": lambda: part_c(args), "D": lambda: part_d(args)}
    for part in "ABCD":
        if part not in parts:
            continue
        ok = steps[part]()
        if not ok and not args.continuar:
            for later in "ABCD"[("ABCD".index(part) + 1):]:
                if later in parts:
                    results.append((f"Parte {later}", SKIP, f"la parte {part} falló; use --continuar para seguir igual"))
            break

    path = write_summary(now, args)
    failed = [r for r in results if r[1] == FAIL]
    warned = [r for r in results if r[1] == WARN]
    header("Resumen")
    for step, status, detail in results:
        say(f"   {status:<8} {step}: {detail}")
    say(f"\n{len(results)} pasos · {len(failed)} fallidos · {len(warned)} avisos · resumen en {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
