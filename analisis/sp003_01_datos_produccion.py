"""SP-003 · Actividad 3: definir qué datos se despliegan en producción y medir su tamaño.

Genera en datos/produccion/ los dos archivos que usará la aplicación:
  - catalogo.csv         bienes que el clasificador puede sugerir (mínimo de SP-002)
  - adjudicaciones.csv   compras de esos bienes, solo con las columnas que usa el analista

Uso:  python sp003_01_datos_produccion.py
"""
import pandas as pd

import reglas as r
from utils import BASE, PROCESADOS, guardar

PRODUCCION = BASE / "datos" / "produccion"
PRODUCCION.mkdir(parents=True, exist_ok=True)

analitica = pd.read_csv(PROCESADOS / "tabla_analitica.csv")
grupos = pd.read_csv(PROCESADOS / "grupos.csv")

# Catálogo: solo los bienes con el mínimo de adjudicaciones de SP-002
catalogo = grupos.loc[
    grupos["n_adjudicaciones"] >= r.MIN_CATALOGO,
    ["codigoitem", "itemcubso", "unidad_medida", "n_adjudicaciones"],
]

# Adjudicaciones: solo las de bienes del catálogo y solo las columnas que usa el analista
COLUMNAS = ["codigoitem", "anio", "codigoconvocatoria", "precio_unitario",
            "n_postores", "entidad_departamento"]
adjudicaciones = analitica.loc[analitica["codigoitem"].isin(catalogo["codigoitem"]), COLUMNAS].copy()
adjudicaciones["n_postores"] = adjudicaciones["n_postores"].astype("Int64")

archivos = {"catalogo.csv": catalogo, "adjudicaciones.csv": adjudicaciones}
filas = []
for nombre, tabla in archivos.items():
    ruta = PRODUCCION / nombre
    tabla.to_csv(ruta, index=False, encoding="utf-8")
    filas.append({
        "archivo": nombre,
        "filas": len(tabla),
        "columnas": tabla.shape[1],
        "mb_en_disco": round(ruta.stat().st_size / 1e6, 2),
        "mb_en_memoria": round(tabla.memory_usage(deep=True).sum() / 1e6, 2),
    })

tamanio = pd.DataFrame(filas)
print("### DATOS DE PRODUCCIÓN")
print(tamanio.to_string(index=False))
print(f"\n  Total en disco: {tamanio['mb_en_disco'].sum():.2f} MB | "
      f"en memoria: {tamanio['mb_en_memoria'].sum():.2f} MB")

# Estimación del catálogo como texto para el modelo de lenguaje (~4 caracteres por token)
caracteres = (catalogo["itemcubso"].str.len() + catalogo["unidad_medida"].str.len() + 12).sum()
print(f"  Catálogo como texto para el modelo: ~{caracteres / 4 / 1000:.0f} mil tokens (estimación)")

guardar(tamanio, "sp003/01_tamanio_datos_produccion.csv")
