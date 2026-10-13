"""Carga los datos de producción en memoria al iniciar la aplicación (SP-003)."""
from pathlib import Path

import pandas as pd

CARPETA = Path(__file__).parent / "datos"

CATALOGO = pd.read_csv(CARPETA / "catalogo.csv")
CODIGOS_VALIDOS = set(CATALOGO["codigoitem"])
BIENES = {
    int(r.codigoitem): {"codigo": int(r.codigoitem), "nombre": r.itemcubso.strip(), "unidad": r.unidad_medida}
    for r in CATALOGO.itertuples()
}

ADJUDICACIONES = pd.read_csv(
    CARPETA / "adjudicaciones.csv",
    dtype={"n_postores": "Int64"},
    float_precision="round_trip",
)