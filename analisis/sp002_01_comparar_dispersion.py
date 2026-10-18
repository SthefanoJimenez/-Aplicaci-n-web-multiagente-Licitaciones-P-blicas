import pandas as pd

from utils import PROCESADOS, guardar

RUBROS = {
    233280: "Combustible",
    12771: "Material de construcción",
    307453: "Alimento",
    65078: "Bien de capital",
}


def cv(x):
    return x.std() / x.mean()


def riq(x):
    return (x.quantile(0.75) - x.quantile(0.25)) / x.median()


analitica = pd.read_csv(PROCESADOS / "tabla_analitica.csv")
filas = []
for codigo, naturaleza in RUBROS.items():
    datos = analitica[analitica["codigoitem"] == codigo]
    x = datos["precio_unitario"]
    q1, q3 = x.quantile([0.25, 0.75])
    iqr = q3 - q1
    atipico = (x < q1 - 1.5 * iqr) | (x > q3 + 1.5 * iqr)
    sin_atipicos = x[~atipico]
    filas.append({
        "naturaleza": naturaleza,
        "bien": datos["itemcubso"].iloc[0],
        "adjudicaciones": len(x),
        "atipicos": int(atipico.sum()),
        "precio_mediano": round(x.median(), 2),
        "precio_maximo": round(x.max(), 2),
        "cv_con_atipicos": round(cv(x), 3),
        "cv_sin_atipicos": round(cv(sin_atipicos), 3),
        "cv_cambio_pct": round(100 * (cv(x) - cv(sin_atipicos)) / cv(sin_atipicos), 1),
        "riq_con_atipicos": round(riq(x), 3),
        "riq_sin_atipicos": round(riq(sin_atipicos), 3),
        "riq_cambio_pct": round(100 * (riq(x) - riq(sin_atipicos)) / riq(sin_atipicos), 1),
    })

tabla = pd.DataFrame(filas)
print(tabla.drop(columns="bien").to_string(index=False))
guardar(tabla, "sp002/01_comparacion_dispersion.csv")
