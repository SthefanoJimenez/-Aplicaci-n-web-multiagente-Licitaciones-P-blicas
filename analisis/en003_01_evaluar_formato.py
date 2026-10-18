import time

import pandas as pd

from utils import CACHE, ruta_excel, guardar

FUENTE, ANIO = "POSTOR", 2024
excel = ruta_excel(FUENTE, ANIO)
pkl = CACHE / f"{FUENTE}{ANIO}.pkl"
csv = CACHE / f"{FUENTE}{ANIO}_prueba.csv"


def medir(funcion):
    inicio = time.perf_counter()
    resultado = funcion()
    return resultado, time.perf_counter() - inicio


print(f"Archivo de prueba: {excel.name}")
df, t_excel = medir(lambda: pd.read_excel(excel))
df.to_pickle(pkl)
df.to_csv(csv, index=False)
_, t_pkl = medir(lambda: pd.read_pickle(pkl))
_, t_csv = medir(lambda: pd.read_csv(csv))

resultados = pd.DataFrame([
    {"formato": "Excel original", "segundos": t_excel, "megabytes": excel.stat().st_size / 1e6},
    {"formato": "Copia rápida (.pkl)", "segundos": t_pkl, "megabytes": pkl.stat().st_size / 1e6},
    {"formato": "CSV", "segundos": t_csv, "megabytes": csv.stat().st_size / 1e6},
]).round(2)
resultados["veces_mas_rapido_que_excel"] = (t_excel / resultados["segundos"]).round(0)
csv.unlink()

print(resultados.to_string(index=False))
guardar(resultados, "en003/01_evaluacion_formato.csv")
