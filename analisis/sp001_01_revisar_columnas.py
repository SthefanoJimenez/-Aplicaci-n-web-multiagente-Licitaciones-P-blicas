import sys

import pandas as pd

from utils import FUENTES, cargar, guardar

anio = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
filas = []

for fuente in FUENTES:
    df = cargar(fuente, anio)
    print(f"\n=== {fuente} {anio}: {len(df):,} filas x {df.shape[1]} columnas ===")
    for col in df.columns:
        s = df[col]
        ejemplo = s.dropna().astype(str).iloc[0][:40] if s.notna().any() else ""
        fila = {
            "fuente": fuente,
            "columna": col,
            "tipo": str(s.dtype),
            "pct_lleno": round(100 * s.notna().mean(), 1),
            "valores_distintos": s.nunique(),
            "ejemplo": ejemplo,
        }
        filas.append(fila)
        print(f"  {col:30s} {fila['tipo']:15s} lleno={fila['pct_lleno']:5.1f}%  "
              f"distintos={fila['valores_distintos']:>7,}  ej: {ejemplo}")

guardar(pd.DataFrame(filas), f"sp001/01_perfil_columnas_{anio}.csv")
