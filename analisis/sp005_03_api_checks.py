"""SP-005 · Pruebas B4 a B9 de la API del OECE en un solo comando.

Hace lo mismo que los pasos B4 a B9 de la guía, sin copiar valores a mano:
  B4  baja el archivo JSON del mes (ZIP)
  B5  ejecuta el perfil de campos (sp005_02_profile_fields.py)
  B6  toma de la muestra el OCID, el tender.id y la URL de las bases
  B7  consulta /record/{ocid}
  B8  consulta /release/seace_v3/{tender.id}
  B9  descarga las bases y revisa su formato (PDF, ZIP o RAR)

Uso, desde analisis/:
    python sp005_03_api_checks.py
    python sp005_03_api_checks.py --muestra 1        (usar la 2.ª convocatoria de la muestra)
    python sp005_03_api_checks.py --sin-descarga     (reusar el ZIP ya bajado)

Salidas en salidas/sp005/pruebas_api/ y un resumen en resumen_b4_b9.txt.
Si el portal responde 403 o entrega una página HTML en lugar del archivo, el
script lo anota y sigue: no reintenta ni intenta forzar el acceso.
"""
import argparse
import io
import json
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "salidas" / "sp005" / "pruebas_api"
SAMPLE_FILE = ROOT / "salidas" / "sp005" / "muestra_bienes.json"
PROFILE_SCRIPT = ROOT / "sp005_02_profile_fields.py"
BASE_URL = "https://contratacionesabiertas.oece.gob.pe/api/v1"
USER_AGENT = "LicitaFacil-SP005/1.0 (proyecto academico UPAO)"
TIMEOUT = 60

lines = []


def say(text=""):
    print(text)
    lines.append(text)


def get(session, url, path):
    """GET que guarda la respuesta en path si llegó bien (HTTP 200); devuelve la respuesta o None si no hubo conexión."""
    try:
        response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as error:
        say(f"   Error de conexión: {error}")
        return None
    saved = response.status_code == 200 and response.content
    if saved:  # solo se guarda lo que llegó bien, para no pisar una descarga anterior
        path.write_bytes(response.content)
    say(f"   HTTP {response.status_code} · {response.headers.get('Content-Type', '?')} · "
        f"{len(response.content):,} bytes" + (f" → {path.name}" if saved else " (no se guardó)"))
    return response


def file_kind(content):
    if content[:5] == b"%PDF-":
        return "pdf"
    if content[:2] == b"PK":
        return "zip"
    if content[:4] == b"Rar!":
        return "rar"
    if content[:3] == b"7z\xbc":
        return "7z"
    if content.lstrip()[:1] == b"<":
        return "html"
    return "otro"


def list_archive(content, kind):
    """Nombres de los archivos dentro de un ZIP o RAR (el RAR se lista con tar)."""
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return archive.namelist()
    with tempfile.NamedTemporaryFile(suffix="." + kind) as tmp:
        tmp.write(content)
        tmp.flush()
        result = subprocess.run(["tar", "-tf", tmp.name], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "tar no pudo leer el archivo").strip())
    return [name for name in result.stdout.splitlines() if name.strip()]


def first_list(data, key):
    """Devuelve data[key] si es lista; si la respuesta es un solo objeto, [data]."""
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return data[key]
    return [data] if isinstance(data, dict) else []


def main():
    now = datetime.now(ZoneInfo("America/Lima"))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--user-agent", default=USER_AGENT)
    parser.add_argument("--anio", type=int, default=now.year)
    parser.add_argument("--mes", type=int, default=now.month)
    parser.add_argument("--muestra", type=int, default=0, help="posición en muestra_bienes.json (0, 1 o 2)")
    parser.add_argument("--sin-descarga", action="store_true", help="reusar el ZIP del mes ya descargado")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent, "Accept": "*/*"})
    say(f"SP-005 · pruebas B4 a B9 · {now:%Y-%m-%d %H:%M} (hora de Lima)")
    say(f"URL base: {base}")

    # B4 · archivo del mes
    zip_path = OUT_DIR / f"04_{args.anio}_{args.mes:02d}.zip"
    say(f"\nB4 · Archivo JSON del mes {args.anio}-{args.mes:02d}")
    if args.sin_descarga and zip_path.exists():
        say(f"   Se reusa {zip_path.name} ({zip_path.stat().st_size:,} bytes)")
    else:
        response = get(session, f"{base}/file/seace_v3/json/{args.anio}/{args.mes:02d}/", zip_path)
        if response is None or response.status_code != 200 or file_kind(response.content) != "zip":
            say("   No se obtuvo el ZIP del mes. Revise el código HTTP de arriba; el resto depende de este paso.")
            return finish(1)
    with zipfile.ZipFile(zip_path) as archive:
        say(f"   Contenido: {', '.join(archive.namelist())}")

    # B5 · perfil de campos
    say("\nB5 · Perfil de campos")
    result = subprocess.run([sys.executable, str(PROFILE_SCRIPT), str(zip_path)], capture_output=True, text=True)
    if result.returncode != 0:
        say("   El perfil falló:\n" + (result.stderr or result.stdout).strip())
        return finish(1)
    say(f"   Listo: ver salidas/sp005/perfil_campos.txt (resumen: {result.stdout.splitlines()[1]})")

    # B6 · valores de la muestra
    say("\nB6 · Convocatoria de la muestra")
    sample = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    if not sample:
        say("   La muestra está vacía: el archivo no trae bienes.")
        return finish(1)
    release = sample[min(args.muestra, len(sample) - 1)]
    tender = release.get("tender") or {}
    ocid, tender_id = release.get("ocid"), tender.get("id")
    bases = [d for d in tender.get("documents") or [] if d.get("documentType") == "biddingDocuments" and d.get("url")]
    say(f"   OCID: {ocid}")
    say(f"   tender.id: {tender_id}")
    say(f"   Título: {tender.get('title')}")
    say(f"   Entidad: {(release.get('buyer') or {}).get('name')}")
    for item in tender.get("items", [])[:5]:
        say(f"   Ítem: {item.get('statusDetails')} | {str(item.get('description', ''))[:70]}")
    period = tender.get("tenderPeriod") or {}
    say(f"   tenderPeriod: {period.get('startDate')} → {period.get('endDate')}")
    for doc in tender.get("documents") or []:
        say(f"   Documento: {doc.get('documentType')} | {doc.get('format')} | {doc.get('title')}")

    # B7 · record
    say("\nB7 · GET /record/{ocid}")
    response = get(session, f"{base}/record/{ocid}", OUT_DIR / "07_record.json")
    if response is not None and response.ok:
        try:
            record = first_list(response.json(), "records")[0]
            say(f"   Claves: {', '.join(record.keys())}")
            say(f"   Versiones: {len(record.get('releases', []))} · compiledRelease: {'sí' if 'compiledRelease' in record else 'no'}")
        except (ValueError, IndexError):
            say("   La respuesta no es el JSON esperado.")

    # B8 · versiones del proceso
    say("\nB8 · GET /release/seace_v3/{tender.id}")
    response = get(session, f"{base}/release/seace_v3/{tender_id}", OUT_DIR / "08_release_tender.json")
    if response is not None and response.ok:
        try:
            for version in first_list(response.json(), "releases"):
                statuses = [i.get("statusDetails") for i in (version.get("tender") or {}).get("items", [])]
                say(f"   {version.get('date')} | {version.get('tag')} | {statuses}")
        except ValueError:
            say("   La respuesta no es JSON.")

    # B9 · bases
    say("\nB9 · Descarga de las bases")
    if not bases:
        say("   Esta convocatoria no publicó bases. Pruebe con --muestra 1 o --muestra 2.")
        return finish(0)
    say(f"   Formato declarado: {bases[0].get('format')} · URL: {bases[0]['url']}")
    response = get(session, bases[0]["url"], OUT_DIR / "09_bases.bin")
    if response is not None and response.status_code != 200:
        if response.status_code in (401, 403):
            say("   El portal no entregó el archivo a un cliente automático. Se anota como resultado; no se fuerza.")
        else:
            say("   No se descargó; se anota el código HTTP como resultado.")
    elif response is not None:
        kind = file_kind(response.content)
        say(f"   Formato real: {kind}")
        if kind in ("zip", "rar", "7z"):
            try:
                names = list_archive(response.content, kind)
                say(f"   Contiene {len(names)} archivo(s), {sum(n.lower().endswith('.pdf') for n in names)} PDF:")
                for name in names[:15]:
                    say(f"     - {name}")
            except (RuntimeError, zipfile.BadZipFile) as error:
                say(f"   No se pudo listar el {kind}: {error}")
        elif kind == "html":
            say("   El portal no entregó el archivo a un cliente automático. Se anota como resultado; no se fuerza.")
    return finish(0)


def finish(code):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "resumen_b4_b9.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nResumen guardado en {OUT_DIR / 'resumen_b4_b9.txt'}")
    return code


if __name__ == "__main__":
    sys.exit(main())