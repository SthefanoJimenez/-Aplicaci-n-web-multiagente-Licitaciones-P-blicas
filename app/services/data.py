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

# Adjudicaciones de los bienes del catálogo. "round_trip" lee cada precio
# exactamente como está escrito en el archivo, sin redondear el último decimal.
AWARDS = pd.read_csv(
    DATA_DIR / "awards.csv",
    dtype={"n_postores": "Int64"},
    float_precision="round_trip",
)