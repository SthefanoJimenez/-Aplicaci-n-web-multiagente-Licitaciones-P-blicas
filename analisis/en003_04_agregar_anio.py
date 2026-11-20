"""EN-003 (paso 4) · Agrega un año nuevo a los datos de producción sin volver a leer los Excel anteriores.

Los Excel de CONOSCE de 2023-2025 no están en el repositorio. Pero app/data/awards_detail.csv ya tiene la
tabla analítica completa de esos años (una fila por adjudicación de bienes, con todas las columnas que usa
la app), y de ella salen exactamente catalog.csv y awards.csv (comprobado el 20/11/2026: 2,023 bienes y
43,464 adjudicaciones, iguales a los de la app). Este script le suma la tabla analítica del año nuevo y
vuelve a calcular lo que depende de todos los años juntos:
  - la unidad de medida mayoritaria de cada código (se descartan las filas en otra unidad);
  - el nombre de cada código (el más frecuente del año más reciente);
  - los atípicos por bien (Tukey sobre el precio unitario);
  - el catálogo (bienes con al menos reglas.MIN_CATALOGO adjudicaciones) y awards.csv.

Diferencia con correr todo desde los Excel: la unidad mayoritaria se calcula sobre lo que ya había pasado ese
filtro en 2023-2025, no sobre los datos crudos de esos años. Solo puede cambiar en códigos con unidades
mezcladas; el script dice cuántas filas descarta por eso.

Uso, desde analisis/, con los tres Excel del año nuevo en datos/crudos/:
    ANIOS=2026 python en003_02_construir_dataset-2.py
    ANIOS=2026 python en003_03_datos_panel.py
    python en003_04_agregar_anio.py
Genera en datos/produccion/app/ los archivos para copiar a app/data/:
    awards_detail.csv, awards.csv, catalog.csv, cleaning_funnel.csv, data_quality.csv
"""
import pandas as pd

import reglas as r
from utils import BASE

APP_DATA = BASE.parent / "app" / "data"
NUEVO = BASE / "datos" / "produccion"            # lo que dejó en003_03 con ANIOS=<año nuevo>
SALIDA = NUEVO / "app"


def main():
    base = pd.read_csv(APP_DATA / "awards_detail.csv", low_memory=False)
    nuevo = pd.read_csv(NUEVO / "awards_detail.csv", low_memory=False)
    repetidos = sorted(set(base["anio"]) & set(nuevo["anio"]))
    if repetidos:
        raise SystemExit(f"Los años {repetidos} ya están en app/data/awards_detail.csv: no se agregan dos veces.")
    print(f"Base: {len(base):,} adjudicaciones ({base['anio'].min()}-{base['anio'].max()}) · "
          f"nuevo: {len(nuevo):,} ({', '.join(map(str, sorted(nuevo['anio'].unique())))})")
    d = pd.concat([base, nuevo], ignore_index=True)

    # Unidad mayoritaria de cada código, con todos los años
    conteo = (d.groupby(["codigoitem", "unidad_medida"]).size().rename("n").reset_index()
              .sort_values(["codigoitem", "n", "unidad_medida"], ascending=[True, False, True]))
    mayoritaria = conteo.drop_duplicates("codigoitem")[["codigoitem", "unidad_medida"]].assign(_ok=True)
    antes = len(d)
    d = d.merge(mayoritaria, on=["codigoitem", "unidad_medida"], how="left")
    d = d[d["_ok"].notna()].drop(columns="_ok").reset_index(drop=True)
    print(f"Filas descartadas por otra unidad de medida al juntar los años: {antes - len(d):,}")

    # Nombre: el más frecuente del año más reciente de cada código
    nombres = (d.groupby(["codigoitem", "anio", "itemcubso"]).size().rename("n").reset_index()
               .sort_values(["codigoitem", "anio", "n", "itemcubso"], ascending=[True, False, False, True])
               .drop_duplicates("codigoitem")[["codigoitem", "itemcubso"]])
    d = d.drop(columns="itemcubso").merge(nombres, on="codigoitem", how="left")

    # Atípicos por bien con todos los años
    q = d.groupby("codigoitem")["precio_unitario"].quantile([0.25, 0.75]).unstack()
    q.columns = ["_p25", "_p75"]
    d = d.join(q, on="codigoitem")
    iqr = d["_p75"] - d["_p25"]
    d["atipico"] = (d["precio_unitario"] < d["_p25"] - 1.5 * iqr) | (d["precio_unitario"] > d["_p75"] + 1.5 * iqr)
    d = d[list(base.columns)].sort_values(["anio", "codigoconvocatoria", "n_item"], kind="stable")
    d["n_postores"] = d["n_postores"].astype("Int64")

    grupos = (d.groupby(["codigoitem", "itemcubso", "unidad_medida"]).size().rename("n_adjudicaciones")
              .reset_index().sort_values("codigoitem"))
    catalogo = grupos[grupos["n_adjudicaciones"] >= r.MIN_CATALOGO]
    awards = d.loc[d["codigoitem"].isin(catalogo["codigoitem"]),
                   ["codigoitem", "unidad_medida", "anio", "codigoconvocatoria", "precio_unitario", "n_postores",
                    "entidad_departamento"]]

    embudo_base = pd.read_csv(APP_DATA / "cleaning_funnel.csv")
    embudo_nuevo = pd.read_csv(NUEVO / "cleaning_funnel.csv")
    embudo = embudo_base.assign(filas=embudo_base["filas"] + embudo_nuevo["filas"])
    embudo.loc[embudo.index[-1], "filas"] = len(d)          # la unidad mayoritaria se calculó con todos los años
    calidad = pd.concat([pd.read_csv(APP_DATA / "data_quality.csv"), pd.read_csv(NUEVO / "data_quality.csv")])

    SALIDA.mkdir(parents=True, exist_ok=True)
    for nombre, tabla in {"awards_detail.csv": d, "awards.csv": awards, "catalog.csv": catalogo,
                          "cleaning_funnel.csv": embudo, "data_quality.csv": calidad}.items():
        tabla.to_csv(SALIDA / nombre, index=False, encoding="utf-8")
        print(f"  {nombre}: {len(tabla):,} filas")
    print(f"\nAdjudicaciones por año: {d.groupby('anio').size().to_dict()}")
    print(f"Catálogo: {len(catalogo):,} bienes (antes {len(pd.read_csv(APP_DATA / 'catalog.csv')):,}) · "
          f"awards.csv: {len(awards):,} filas")
    print(f"Guardado en {SALIDA.relative_to(BASE)}/: cópielo a app/data/.")


if __name__ == "__main__":
    main()