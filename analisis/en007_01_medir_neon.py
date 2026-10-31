"""EN-007 · Mediciones de la base de convocatorias: arranque en frío de Neon y conteos de OE8.

Uso, desde analisis/ (con DATABASE_URL en app/.env o en el entorno):

    python en007_01_medir_neon.py arranque --inactivo 10
        Mide cuánto tarda la primera consulta (conectar + SELECT 1) y la segunda. Córralo
        después de que la base estuvo sin uso más de 5 minutos (Neon apaga el cómputo a los
        5 minutos) e indique con --inactivo cuántos minutos pasaron. Agrega una fila a
        salidas/en007/arranque_neon.csv. Conviene repetirlo 3 veces en momentos distintos.

    python en007_01_medir_neon.py oe8
        Lista las ejecuciones de la ingesta (actualizaciones) y calcula el desfase entre la
        publicación de cada convocatoria y la primera vez que la vio el sistema (meta de OE8:
        24 horas o menos). Escribe salidas/en007/oe8_ejecuciones.csv y oe8_desfase.csv.

No imprime la cadena de conexión.
"""
import argparse
import csv
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT.parent / "app"
OUTPUT_DIR = ROOT / "salidas" / "en007"
sys.path.insert(0, str(APP_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(APP_DIR / ".env")

import config  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from services import db  # noqa: E402
from services.ingest import as_utc  # noqa: E402


def engine_or_exit():
    if not config.DATABASE_URL:
        sys.exit("Falta DATABASE_URL en app/.env o en el entorno.")
    return db.make_engine()


def append_row(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        if new:
            writer.writeheader()
        writer.writerow(row)


def command_cold_start(args):
    timings = []
    for _ in range(2):
        engine = engine_or_exit()     # motor nuevo: la primera consulta abre una conexión nueva
        begin = time.perf_counter()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        timings.append(round(time.perf_counter() - begin, 2))
        engine.dispose()
    row = {"fecha": datetime.now().astimezone().isoformat(timespec="seconds"), "minutos_inactivo": args.inactivo,
           "primera_consulta_s": timings[0], "segunda_consulta_s": timings[1]}
    append_row(OUTPUT_DIR / "arranque_neon.csv", row)
    print(f"Primera consulta: {timings[0]} s · segunda: {timings[1]} s · inactiva antes: {args.inactivo} min")
    print(f"Agregado a {OUTPUT_DIR / 'arranque_neon.csv'}")


def command_oe8(_args):
    engine = engine_or_exit()
    factory = db.make_session_factory(engine)
    with factory() as session:
        runs = session.scalars(select(db.IngestionRun).order_by(db.IngestionRun.started_at)).all()
        tenders = session.scalars(select(db.Tender)).all()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "oe8_ejecuciones.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["id", "inicio_utc", "fin_utc", "estado", "origen", "meses", "vigentes_fuente_pais",
                         "vigentes_fuente_pilotos", "vigentes_guardadas", "nuevas", "actualizadas", "cerradas", "error"])
        for r in runs:
            writer.writerow([r.id, as_utc(r.started_at), as_utc(r.finished_at), r.status, r.trigger, r.months,
                             r.source_open_goods, r.source_open_pilot, r.saved_open, r.new_count, r.updated_count,
                             r.closed_count, (r.error or "")[:200]])
    ok = [r for r in runs if r.status == "ok"]
    print(f"Ejecuciones: {len(runs)} · correctas: {len(ok)} · fallidas: {sum(r.status == 'fallida' for r in runs)}")
    for r in ok[-5:]:
        print(f"  {as_utc(r.started_at):%Y-%m-%d %H:%M} UTC · fuente {r.source_open_pilot} · guardadas {r.saved_open}")

    # El desfase solo tiene sentido para lo que apareció DESPUÉS de la primera ejecución correcta:
    # la primera carga "ve por primera vez" convocatorias publicadas semanas antes.
    first_ok = ok[0].id if ok else None
    lags = []
    for t in tenders:
        if t.first_run_id == first_ok or not t.published_at or not t.first_seen_at:
            continue
        hours = (as_utc(t.first_seen_at) - as_utc(t.published_at)).total_seconds() / 3600
        lags.append((t.code, as_utc(t.published_at), as_utc(t.first_seen_at), round(hours, 1)))
    with open(OUTPUT_DIR / "oe8_desfase.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["codigo", "publicada_utc", "vista_por_primera_vez_utc", "desfase_horas"])
        writer.writerows(lags)
    if lags:
        hours = [h for *_, h in lags]
        within = sum(h <= 24 for h in hours)
        print(f"Desfase (convocatorias nuevas después de la primera carga): {len(hours)} · mediana "
              f"{statistics.median(hours):.1f} h · con 24 h o menos: {within} ({100 * within / len(hours):.0f} %)")
    else:
        print("Todavía no hay convocatorias nuevas después de la primera carga para medir el desfase.")
    print(f"Detalle en {OUTPUT_DIR}")
    engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    cold = sub.add_parser("arranque", help="Arranque en frío de Neon.")
    cold.add_argument("--inactivo", type=int, required=True, help="Minutos que la base estuvo sin uso antes de medir.")
    sub.add_parser("oe8", help="Ejecuciones y desfase de OE8.")
    args = parser.parse_args()
    {"arranque": command_cold_start, "oe8": command_oe8}[args.command](args)


if __name__ == "__main__":
    main()