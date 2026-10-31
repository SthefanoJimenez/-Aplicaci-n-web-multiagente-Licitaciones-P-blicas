"""SP-006 · ¿Bastan 2 meses de archivos o hace falta un tercero? (actividad 7)

Descarga hoy el archivo mensual OCDS del mes actual y de los dos anteriores (los mismos
que usa el SP-005, con el mismo nombre de cliente) y compara las convocatorias de bienes
vigentes que salen al leer 2 meses frente a 3. Una convocatoria que solo aparece con el
tercer mes tuvo su última novedad hace más de dos meses y sigue en estado CONVOCADO.

No modifica las salidas del SP-005: escribe en salidas/sp006/.

Uso, desde analisis/:
    python sp006_03_comparar_meses.py
    python sp006_03_comparar_meses.py --from-file mes_actual.zip mes_anterior.zip mes_tercero.zip

Salidas: salidas/sp006/meses_comparacion.csv (una fila por convocatoria que aporta el tercer mes)
y el resumen en pantalla.
"""
import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

import sp005_01_tender_source as sp

OUTPUT_DIR = Path(__file__).resolve().parent / "salidas" / "sp006"
PILOTS = {"1510", "2510"}


def open_goods(paths, now):
    """Bienes vigentes al leer estos archivos mensuales (versiones unidas por ocid, como en el SP-005)."""
    objects = []
    for path in paths:
        objects.extend(sp.read_ocds_file(path))
    rows = [sp.normalize_release(r) for r in sp.compiled_releases(objects)]
    return {r["ocid"]: r for r in rows if sp.is_goods(r) and sp.is_open(r, now)}


def rubros(row):
    return {sp.family_of(i) for i in row["items"]} - {None}


def compare(two, three):
    """Qué aporta el tercer mes: nuevas vigentes y las que cambian de estado al sumar historia."""
    extra = [three[o] for o in three.keys() - two.keys()]
    lost = [two[o] for o in two.keys() - three.keys()]
    pilot = lambda rows: [r for r in rows if rubros(r) & PILOTS]
    return {
        "vigentes_2_meses": len(two), "vigentes_3_meses": len(three),
        "piloto_2_meses": len(pilot(two.values())), "piloto_3_meses": len(pilot(three.values())),
        "aporta_tercer_mes": len(extra), "aporta_tercer_mes_piloto": len(pilot(extra)),
        "dejan_de_estar_vigentes": len(lost), "extra": extra,
    }


def main():
    sp.safe_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-agent", default=sp.DEFAULT_USER_AGENT)
    parser.add_argument("--from-file", nargs=3, metavar=("ACTUAL", "ANTERIOR", "TERCERO"),
                        help="Tres archivos mensuales ya descargados, del más reciente al más antiguo.")
    args = parser.parse_args()
    now = datetime.now(sp.LIMA)
    if args.from_file:
        paths = [Path(p) for p in args.from_file]
    else:
        session = sp.session_for(sp.BASE_URL, args.user_agent)
        # refresh=True: el archivo del mes se regenera a diario, la caché del 17/10 ya no sirve
        paths = [sp.download_month(session, y, m, refresh=True) for y, m in sp.months_to_fetch(now, 3)]
    two = open_goods(paths[:2], now)
    three = open_goods(paths, now)
    result = compare(two, three)

    print(f"Archivos: {', '.join(p.name for p in paths)}")
    print(f"Bienes vigentes con 2 meses: {result['vigentes_2_meses']:,} · con 3 meses: {result['vigentes_3_meses']:,}")
    print(f"En los rubros piloto: {result['piloto_2_meses']} con 2 meses · {result['piloto_3_meses']} con 3 meses")
    print(f"El tercer mes aporta {result['aporta_tercer_mes']} vigentes ({result['aporta_tercer_mes_piloto']} de los pilotos)")
    if result["dejan_de_estar_vigentes"]:
        print(f"Aviso: {result['dejan_de_estar_vigentes']} dejan de figurar como vigentes al sumar el tercer mes; revisar.")
    by_month = Counter((sp.parse_date(r["published"]) or now).strftime("%Y-%m") for r in result["extra"])
    if by_month:
        print("Mes de publicación de lo que aporta el tercer mes: " + ", ".join(f"{k}: {v}" for k, v in sorted(by_month.items())))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"ocid": r["ocid"], "codigo": r["code"], "rubros": " ".join(sorted(rubros(r))),
                   "metodo": r["method"], "publicada": r["published"], "entidad": r["entity"],
                   "departamento": r["department"]} for r in result["extra"]]
                 ).to_csv(OUTPUT_DIR / "meses_comparacion.csv", index=False, encoding="utf-8-sig")
    print(f"Detalle: {OUTPUT_DIR / 'meses_comparacion.csv'}")


if __name__ == "__main__":
    main()