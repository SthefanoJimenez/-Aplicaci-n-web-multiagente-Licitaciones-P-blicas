"""SP-006 · Recuperar las 13 bases del spike en una computadora sin la carpeta del 17/10.

El SP-006 trabaja con las bases que descargó el SP-005 el 17/10 (salidas/sp005/). Si esa
carpeta no está en esta computadora, este script vuelve a descargar SOLO esas 13 bases:

  1. consulta cada convocatoria por su cuenta con GET /api/v1/record/{ocid} (una consulta pequeña
     por base, el endpoint que el SP-005 probó como B7), estén o no vigentes hoy. Con --mensual,
     en cambio, descarga los archivos mensuales completos como el SP-005;
  2. usa el mismo nombre de cliente del SP-005;
  3. de cada una toma el mismo documento que el 17/10 (por ejemplo "Bases Administrativas"),
     aunque hoy ya existan otras bases;
  4. lo guarda en salidas/sp005/bases/ y agrega o reemplaza sus filas en documentos.csv y
     vigentes.json. Las demás filas de esos archivos no se tocan.

Antes de escribir guarda una copia de documentos.csv y vigentes.json (una sola vez).

No vuelve a ejecutar el SP-005: no cambia la muestra ni las conclusiones del 17/10.

Uso, desde analisis/:
    python sp006_04_recuperar_bases.py                      las 13 bases del SP-006 (por /record)
    python sp006_04_recuperar_bases.py --codigos "1258127,1257868"
    python sp006_04_recuperar_bases.py --sin-descargar      usa los archivos mensuales ya descargados
    python sp006_04_recuperar_bases.py --from-file octubre.json septiembre.json
                                                            archivos mensuales bajados en el navegador

Salida en pantalla: qué se recuperó, con qué documento, y si cambió frente al 17/10.
"""
import argparse
import json
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

import sp005_01_tender_source as sp

# Documento que midió el SP-005 el 17/10 para cada base (columna "documento" de bases_sp006.csv).
DOCUMENT_17OCT = {
    "1255745": "Bases Administrativas", "1253677": "Bases Integradas", "1258393": "Bases Administrativas",
    "1255636": "Bases Administrativas", "1257402": "Bases Administrativas", "1258093": "Bases Administrativas",
    "1254843": "Bases Administrativas", "1250704": "Bases Integradas", "1257702": "Bases Administrativas",
    "1258420": "Bases Administrativas", "1257868": "Bases Administrativas", "1258127": "Bases Administrativas",
    "1258029": "Bases Administrativas",
}

# OCID de cada base (columna "ocid" de bases_sp006.csv del 28/10). 1253677 no sigue el patrón.
OCID_17OCT = {code: f"ocds-dgv273-seacev3-{code}" for code in DOCUMENT_17OCT}
OCID_17OCT["1253677"] = "ocds-dgv273-seacev3-2026-1894-9"
RECORD_PATH = "/api/v1/record/{ocid}"


def ocid_for_code(code):
    if code in OCID_17OCT:
        return OCID_17OCT[code]
    if code.startswith("ocds-"):
        return code
    if code.startswith("SIE-"):
        parts = code.split("-")
        if len(parts) >= 2:
            return f"ocds-dgv273-seacev3-{parts[1]}"
    return f"ocds-dgv273-seacev3-{code}"


def rows_by_record(session, codes):
    """Cada convocatoria por su cuenta, con GET /record/{ocid} (una consulta pequeña por base).

    Es el endpoint que el SP-005 probó como B7. Evita bajar los archivos mensuales completos.
    Devuelve {código: fila normalizada como en el SP-005}.
    """
    rows = {}
    for code in codes:
        ocid = ocid_for_code(code)
        response = session.get(session.base_url + RECORD_PATH.format(ocid=ocid), timeout=sp.TIMEOUT_SECONDS)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        data = response.json()
        records = data.get("records") if isinstance(data, dict) else data
        record = (records or [{}])[0] if isinstance(records, list) else (data if isinstance(data, dict) else {})
        release = record.get("compiledRelease") or sp._merge_releases(record.get("releases") or [])
        if release:
            row = sp.normalize_release(release)
            rows[code] = row
            if str(row.get("code")) != code:
                rows[str(row.get("code"))] = row
        time.sleep(sp.REQUEST_PAUSE_SECONDS)
    return rows


def same_document_as_17oct(row, code):
    """La convocatoria con sus documentos limitados al mismo tipo de bases del 17/10, si existe.

    Si hoy ya hay "Bases Integradas", el SP-005 las preferiría; aquí se mantiene el documento del
    17/10 para que las páginas del catálogo sigan valiendo. Devuelve (fila, aviso).
    """
    chosen = sp.bases_document(row)
    if not chosen:
        return row, "sin documentos con enlace"
    if code not in DOCUMENT_17OCT:   # un código que no es del SP-006: la regla del SP-005, sin aviso
        return row, ""
    wanted = sp.normalize(DOCUMENT_17OCT[code])
    matching = [d for d in row["documents"] if d.get("url") and sp.normalize(d.get("title")) == wanted]
    if matching:
        return dict(row, documents=matching), ""
    return row, f"no se halló \"{DOCUMENT_17OCT[code]}\"; se usa \"{chosen.get('title')}\""


def backup_once(path):
    copy = path.with_name(f"{path.stem}_antes_sp006_04{path.suffix}")
    if path.exists() and not copy.exists():
        shutil.copy2(path, copy)
        print(f"Copia de respaldo: {copy}")


def merge_outputs(folder, measured, rows_by_code):
    """Agrega o reemplaza las filas de los códigos recuperados; las demás quedan igual."""
    documents_csv, vigentes_json = folder / "documentos.csv", folder / "vigentes.json"
    backup_once(documents_csv)
    backup_once(vigentes_json)
    codes = set(measured["codigo"].astype(str))
    if documents_csv.exists():
        old = pd.read_csv(documents_csv, dtype={"codigo": str})
        measured = pd.concat([old[~old["codigo"].isin(codes)], measured], ignore_index=True)
    measured.to_csv(documents_csv, index=False, encoding="utf-8-sig")
    ocids = {rows_by_code[c]["ocid"] for c in codes}
    old_rows = json.loads(vigentes_json.read_text(encoding="utf-8")) if vigentes_json.exists() else []
    merged = [r for r in old_rows if r.get("ocid") not in ocids] + [rows_by_code[c] for c in sorted(codes)]
    vigentes_json.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    return len(measured), len(merged)


def probe_api(args):
    """Diagnóstico del 403: la misma consulta a /files con 6 variantes de cliente, sin descargas.

    Todas usan el nombre de cliente indicado (por defecto, DEFAULT_USER_AGENT de sp005_01; se cambia
    con --user-agent). Sirve para ver qué acepta hoy el portal antes de decidir con qué nombre se
    identifica la app (ver SP-006_nota.md, sección "Fuente").
    """
    session = sp.session_for(args.base_url, args.user_agent)
    now = datetime.now(sp.LIMA)
    files_url = session.base_url + sp.FILES_LIST_PATH.format(page=1)
    month_url = session.base_url + sp.MONTHLY_JSON_PATH.format(year=now.year, month=f"{now.month:02d}")
    agent = session.headers["User-Agent"]
    print(f"Cliente: {agent}")
    variants = [
        ("1 actual del SP-005 (Accept */*, Referer)", files_url, dict(session.headers)),
        ("2 cliente de API: Accept application/json", files_url, {"User-Agent": agent, "Accept": "application/json"}),
        ("3 cliente de API: Accept json y español", files_url,
         {"User-Agent": agent, "Accept": "application/json", "Accept-Language": "es-PE,es;q=0.9"}),
        ("4 archivo del mes, Accept application/json", month_url, {"User-Agent": agent, "Accept": "application/json"}),
    ]
    results = []
    for label, url, headers in variants:
        try:
            with requests.get(url, headers=headers, timeout=sp.TIMEOUT_SECONDS, stream=True) as response:
                results.append((label, response.status_code, response.headers.get("Content-Type", "?")))
        except requests.RequestException as error:
            results.append((label, f"sin respuesta ({type(error).__name__})", ""))
        time.sleep(sp.REQUEST_PAUSE_SECONDS)
    # 5: otra biblioteca HTTP (la estándar de Python), mismo nombre de cliente y Accept json
    try:
        request = urllib.request.Request(files_url, headers={"User-Agent": agent, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=sp.TIMEOUT_SECONDS) as response:
            results.append(("5 urllib (otra biblioteca), Accept json", response.status, response.headers.get("Content-Type", "?")))
    except urllib.error.HTTPError as error:
        results.append(("5 urllib (otra biblioteca), Accept json", error.code, error.headers.get("Content-Type", "?")))
    except (urllib.error.URLError, OSError) as error:
        results.append(("5 urllib (otra biblioteca), Accept json", f"sin respuesta ({type(error).__name__})", ""))
    # 6: el proceso de una base por su cuenta (/record), como la prueba B7 del SP-005
    record_url = session.base_url + RECORD_PATH.format(ocid=OCID_17OCT["1258127"])
    try:
        with session.get(record_url, timeout=sp.TIMEOUT_SECONDS, stream=True) as response:
            results.append(("6 /record de una convocatoria", response.status_code, response.headers.get("Content-Type", "?")))
    except requests.RequestException as error:
        results.append(("6 /record de una convocatoria", f"sin respuesta ({type(error).__name__})", ""))
    for label, status, kind in results:
        print(f"  {status} · {kind or '?'} · {label}")
    ok = [label for label, status, _ in results if status == 200]
    if ok and not results[0][1] == 200:
        print(f"\nFunciona: {ok[0]}. Envíen este resultado para aplicar ese cambio al SP-005.")
    elif not ok:
        print("\nNinguna variante funciona con este nombre de cliente. Si en el navegador la misma dirección abre, "
              "el portal hoy no acepta este cliente (ver SP-006_nota.md, sección \"Fuente\").")


def main():
    sp.safe_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codigos", help="Códigos separados por comas (por defecto, las 13 bases del SP-006).")
    parser.add_argument("--mensual", action="store_true",
                        help="Buscar en los archivos mensuales completos en lugar de /record (como el SP-005).")
    parser.add_argument("--sin-descargar", action="store_true",
                        help="Usa los archivos mensuales de datos/cache/sp005 sin descargarlos de nuevo.")
    parser.add_argument("--from-file", nargs="+", metavar="ARCHIVO",
                        help="Archivos mensuales JSON descargados a mano desde el navegador (mes actual y anterior).")
    parser.add_argument("--base-url", default=sp.BASE_URL)
    parser.add_argument("--user-agent", default=sp.DEFAULT_USER_AGENT)
    parser.add_argument("--probar-api", action="store_true",
                        help="Diagnóstico del 403: 6 consultas pequeñas con variantes honestas de cliente de API, sin descargas.")
    args = parser.parse_args()
    if args.probar_api:
        probe_api(args)
        return

    codes = [c.strip() for c in args.codigos.split(",")] if args.codigos else list(DOCUMENT_17OCT)
    now = datetime.now(sp.LIMA)
    session = sp.session_for(args.base_url, args.user_agent)
    # refresh: la caché de esta computadora puede ser anterior al 17/10 y no traer estas convocatorias
    paths = []
    try:
        if args.from_file:
            paths = [Path(p).expanduser() for p in args.from_file]
        elif args.mensual or args.sin_descargar:
            paths = [sp.download_month(session, y, m, refresh=not args.sin_descargar) for y, m in sp.months_to_fetch(now, 2)]
        else:
            rows_by_code = rows_by_record(session, codes)
    except requests.RequestException as error:
        # No se reintenta ni se cambia el nombre de cliente: si el portal rechaza la consulta, se informa y se para.
        status = getattr(getattr(error, "response", None), "status_code", None)
        print(f"\nNo se pudo consultar la API: {error}")
        if status == 403:
            print("El portal rechazó la consulta (403). Comprueben si la API responde hoy con:\n"
                  "  python3 sp006_04_recuperar_bases.py --probar-api\n"
                  "Si sigue en 403, descarguen las bases a mano desde el SEACE y léanlas con\n"
                  "  python3 sp006_01_leer_bases.py --archivo CODIGO=RUTA  (ver SP-006_pasos.md, 0b).")
        raise SystemExit(1)
    if paths:
        objects = []
        for path in paths:
            objects.extend(sp.read_ocds_file(path))
        rows = [sp.normalize_release(r) for r in sp.compiled_releases(objects)]
        rows_by_code = {str(r["code"]): r for r in rows if str(r.get("code")) in set(codes)}

    missing = [c for c in codes if c not in rows_by_code]
    if missing:
        where = ", ".join(p.name for p in paths) if paths else "/record"
        print(f"Aviso: no se encontraron en {where}: {', '.join(missing)}")

    sample, notes = [], {}
    for code in codes:
        if code not in rows_by_code:
            continue
        row, note = same_document_as_17oct(rows_by_code[code], code)
        sample.append(row)
        open_today = sp.is_open(rows_by_code[code], now)
        notes[code] = "; ".join(x for x in [note, "" if open_today else "hoy ya no está vigente"] if x)
    if not sample:
        print("No se recuperó ninguna base.")
        return

    print(f"Descargando {len(sample)} bases …")
    measured = sp.measure_bases(session, sample, sp.OUTPUT_DIR / "bases")
    measured["codigo"] = measured["codigo"].astype(str)
    total_docs, total_json = merge_outputs(sp.OUTPUT_DIR, measured, rows_by_code)

    ok = measured[measured["descargable"]]
    print(f"\nBases recuperadas: {len(ok)} de {len(codes)}")
    for _, r in measured.iterrows():
        status = r.get("formato_real") if r["descargable"] else (r["error"] or "sin enlace")
        extra = f" · {notes[r['codigo']]}" if notes.get(r["codigo"]) else ""
        print(f"  {r['codigo']}: {r['documento']} · {status}{extra}")
    print(f"documentos.csv: {total_docs} filas · vigentes.json: {total_json} convocatorias · en {sp.OUTPUT_DIR}")
    print("Siguiente paso: python sp006_01_leer_bases.py --codigos \"...\" (ver SP-006_pasos.md).")


if __name__ == "__main__":
    main()