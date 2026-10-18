"""Servicio de datos: carga los datos de producción en memoria al iniciar la aplicación (SP-003).

Los nombres de las columnas de los CSV se mantienen como en la fuente del OECE
(codigoitem, itemcubso, unidad_medida…) para conservar la trazabilidad con el análisis.
"""
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

CATALOG = pd.read_csv(DATA_DIR / "catalog.csv")
VALID_CODES = set(CATALOG["codigoitem"])
GOODS = {
    int(row.codigoitem): {"code": int(row.codigoitem), "name": row.itemcubso.strip(), "unit": row.unidad_medida}
    for row in CATALOG.itertuples()
}
# Adjudicaciones por bien según el catálogo (para mostrarlas junto a los candidatos)
N_AWARDS = {int(row.codigoitem): int(row.n_adjudicaciones) for row in CATALOG.itertuples()}

# Adjudicaciones de los bienes del catálogo. "round_trip" lee cada precio
# exactamente como está escrito en el archivo, sin redondear el último decimal.
# El rango de precios (HU-002) y la competencia (HU-003) se calculan con este archivo.
AWARDS = pd.read_csv(
    DATA_DIR / "awards.csv",
    dtype={"n_postores": "Int64"},
    float_precision="round_trip",
)

# Detalle de las 60,024 adjudicaciones de bienes de la tabla analítica (detalle del mercado y panorama):
# entidad, lugar de entrega, proveedor, monto y tipo de proceso. Lo usan el panorama
# del mercado y el detalle del bien. Los proveedores con RUC 10 (personas naturales)
# vienen como "Persona natural".
AWARDS_DETAIL = pd.read_csv(
    DATA_DIR / "awards_detail.csv",
    dtype={"n_postores": "Int64", "proceso": "string", "proveedor": "string",
           "entidad": "string", "provincia_entrega": "string", "departamento_entrega": "string"},
)

# Pasos de limpieza de EN-003 y calidad por año (DO-002), para "Cómo preparamos los datos"
CLEANING_FUNNEL = pd.read_csv(DATA_DIR / "cleaning_funnel.csv")
DATA_QUALITY = pd.read_csv(DATA_DIR / "data_quality.csv")