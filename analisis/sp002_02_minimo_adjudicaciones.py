import numpy as np
import pandas as pd

from utils import PROCESADOS, guardar

TAMANIOS = [3, 5, 8, 10, 15, 20, 30, 50]
REPETICIONES = 200
rng = np.random.default_rng(42)   

analitica = pd.read_csv(PROCESADOS / "tabla_analitica.csv")
grupos = pd.read_csv(PROCESADOS / "grupos.csv")
referencia = grupos.loc[grupos["n_adjudicaciones"] >= 100, "codigoitem"]
precios = {c: analitica.loc[analitica["codigoitem"] == c, "precio_unitario"].to_numpy()
           for c in referencia}
print(f"Bienes de referencia (>= 100 adjudicaciones): {len(precios)}")

filas = []
for n in TAMANIOS:
    errores = []
    for x in precios.values():
        mediana_real = np.median(x)
        for _ in range(REPETICIONES):
            muestra = rng.choice(x, n, replace=False)
            errores.append(abs(np.median(muestra) - mediana_real) / mediana_real)
    errores = np.array(errores)
    filas.append({
        "n": n,
        "bienes_con_al_menos_n": int((grupos["n_adjudicaciones"] >= n).sum()),
        "error_tipico_pct": round(100 * np.median(errores), 1),
        "error_peor_10pct_pct": round(100 * np.quantile(errores, 0.9), 1),
    })

tabla = pd.DataFrame(filas)
print(tabla.to_string(index=False))
guardar(tabla, "sp002/02_error_segun_adjudicaciones.csv")
