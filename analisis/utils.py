"""Funciones compartidas por los scripts de SP-001."""
import os
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
DATOS = BASE / "datos" / "crudos"
CACHE = BASE / "datos" / "cache"
PROCESADOS = BASE / "datos" / "procesados"
SALIDAS = BASE / "salidas"

ANIOS = [2023, 2024, 2025, 2026]   # 2026: convocatorias de enero a octubre (descarga del 20/11/2026)
# ANIOS=2026 en el entorno procesa solo ese año (para agregarlo con en003_04_agregar_anio.py sin los
# Excel de los años anteriores). Ejemplo: ANIOS=2026 python en003_02_construir_dataset-2.py
if os.environ.get("ANIOS", "").strip():
    ANIOS = [int(a) for a in os.environ["ANIOS"].replace(" ", "").split(",") if a]

# Nombre de la columna de convocatoria en cada fuente (en POSTOR lleva guion bajo)
FUENTES = {
    "ADJUDICACIONES": "codigoconvocatoria",
    "CONVOCATORIAS": "codigoconvocatoria",
    "POSTOR": "codigo_convocatoria",
}


# Columnas que usa el proyecto en cada fuente
COLUMNAS_USADAS = {
    "ADJUDICACIONES": [
        "codigoconvocatoria", "n_item", "objetocontractual", "tipoprocesoseleccion",
        "moneda", "cantidad_adjudicado_item", "monto_adjudicado_item_soles",
        "monto_referencial_item_soles", "unidad_medida", "entidad_departamento",
    ],
    "CONVOCATORIAS": ["codigoconvocatoria", "n_item", "codigoitem", "itemcubso"],
    "POSTOR": ["codigo_convocatoria", "n_item", "ruc_codigo_postor"],
}


def ruta_excel(fuente, anio):
    return DATOS / f"CONOSCE_{fuente}{anio}_0.xlsx"


def normalizar_columnas(df):
    """Nombres de columna en minúsculas y con guion bajo.

    Los Excel de 2026 traen los encabezados en MAYÚSCULAS y alguno con espacio («RUC PROVEEDOR»);
    los de 2023-2025 venían en minúsculas. Así los scripts usan los mismos nombres para todos los años.
    """
    return df.rename(columns=lambda c: str(c).strip().lower().replace(" ", "_"))


def cargar(fuente, anio):
    """Lee el Excel una sola vez y guarda una copia rápida en datos/cache/."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"{fuente}{anio}.pkl"
    if cache.exists():
        return normalizar_columnas(pd.read_pickle(cache))
    print(f"  Leyendo {ruta_excel(fuente, anio).name} (la primera vez tarda)...")
    df = normalizar_columnas(pd.read_excel(ruta_excel(fuente, anio)))
    df.to_pickle(cache)
    return df


def llave(df, fuente):
    """Llave de cruce: codigo de convocatoria + numero de item."""
    return df[FUENTES[fuente]].astype(str) + "|" + df["n_item"].astype(str)


def guardar(df, nombre):
    """Guarda en salidas/. El nombre puede incluir subcarpeta, p. ej. 'sp001/x.csv'."""
    ruta = SALIDAS / nombre
    ruta.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(ruta, index=False, encoding="utf-8-sig")
    print(f"  -> Guardado en {ruta.relative_to(BASE)}")