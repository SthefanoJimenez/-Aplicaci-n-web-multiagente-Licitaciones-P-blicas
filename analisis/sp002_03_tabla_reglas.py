import itertools

import pandas as pd

import reglas as r
from utils import PROCESADOS, guardar

analitica = pd.read_csv(PROCESADOS / "tabla_analitica.csv")


def estadisticas(datos):
    return datos.groupby("codigoitem")["precio_unitario"].agg(
        n="size",
        p25=lambda s: s.quantile(0.25),
        p50="median",
        p75=lambda s: s.quantile(0.75),
    )


def resultado(n, riq):
    evidencia = r.resultado_evidencia(n)
    if evidencia != "Con veredicto":
        return evidencia
    return f"Confianza {r.nivel_confianza(n, riq).lower()}"

bandas_n = {
    f"{r.MIN_CONFIANZA_ALTA} o más": r.MIN_CONFIANZA_ALTA,
    f"{r.MIN_VEREDICTO} a {r.MIN_CONFIANZA_ALTA - 1}": r.MIN_VEREDICTO,
    f"{r.MIN_CATALOGO} a {r.MIN_VEREDICTO - 1}": r.MIN_CATALOGO,
    f"Menos de {r.MIN_CATALOGO}": 0,
}
bandas_disp = {
    f"Baja (≤ {r.DISPERSION_BAJA_HASTA})": 0.0,
    f"Media (≤ {r.DISPERSION_MEDIA_HASTA})": r.DISPERSION_MEDIA_HASTA,
    f"Alta (> {r.DISPERSION_MEDIA_HASTA})": 10.0,
}
tabla_confianza = pd.DataFrame(
    [{"adjudicaciones": bn, **{bd: resultado(n, d) for bd, d in bandas_disp.items()}}
     for bn, n in bandas_n.items()]
)
print("### TABLA DE CONFIANZA")
print(tabla_confianza.to_string(index=False))

valores_n = [0, 1, 4, 5, 9, 10, 29, 30, 100]
valores_riq = [0.0, 0.25, 0.2501, 0.5, 0.5001, 5.0]
validos = ["Confianza alta", "Confianza media", "Confianza baja",
           "Sin evidencia suficiente", "Sin evidencia para evaluar"]
combinaciones = [resultado(n, d) for n, d in itertools.product(valores_n, valores_riq)]
print("\n### VERIFICACIÓN DE COMPLETITUD")
print(f"  Combinaciones de evidencia y dispersión probadas: {len(combinaciones)}, "
      f"todas con regla válida: {all(c in validos for c in combinaciones)}")

p25, p75 = 25.0, 30.0
casos_veredicto = {24.99: "Participa", 25.0: "Participa con cautela", 27.5: "Participa con cautela",
                   30.0: "Participa con cautela", 30.01: "No rentable"}
correctos = all(r.veredicto(c, p25, p75) == esperado for c, esperado in casos_veredicto.items())
print(f"  Casos de veredicto probados (incluidos los límites P25 y P75): "
      f"{len(casos_veredicto)}, todos correctos: {correctos}")

st = estadisticas(analitica)
st["riq"] = r.dispersion(st["p25"], st["p50"], st["p75"])
st["resultado"] = [resultado(n, d) for n, d in zip(st["n"], st["riq"])]
distribucion = st["resultado"].value_counts().reindex(validos).fillna(0).astype(int)
distribucion = distribucion.rename("bienes").reset_index().rename(columns={"index": "resultado"})
print("\n### BIENES POR RESULTADO (2023-2025)")
print(distribucion.to_string(index=False))
print(f"  Bienes con exactamente un resultado: {st['resultado'].notna().sum()} de {len(st)}")

entrenamiento = estadisticas(analitica[analitica["anio"] < 2025])
entrenamiento["riq"] = r.dispersion(entrenamiento["p25"], entrenamiento["p50"], entrenamiento["p75"])
entrenamiento["resultado"] = [resultado(n, d) for n, d in zip(entrenamiento["n"], entrenamiento["riq"])]
prueba = analitica[analitica["anio"] == 2025].merge(entrenamiento.reset_index(), on="codigoitem")
prueba = prueba[prueba["resultado"].str.startswith("Confianza")]
prueba["error"] = (prueba["precio_unitario"] - prueba["p50"]).abs() / prueba["precio_unitario"]
verificacion = (
    prueba.groupby("resultado")
    .agg(bienes=("codigoitem", "nunique"), casos_2025=("error", "size"), error_mediano=("error", "median"))
    .reindex(["Confianza alta", "Confianza media", "Confianza baja"])
    .reset_index()
)
verificacion["error_mediano"] = (100 * verificacion["error_mediano"]).round(1)
print("\n### VERIFICACIÓN PRELIMINAR: ERROR DE LA MEDIANA POR NIVEL (%)")
print(verificacion.to_string(index=False))

guardar(tabla_confianza, "sp002/03_tabla_confianza.csv")
guardar(distribucion, "sp002/03_bienes_por_resultado.csv")
guardar(verificacion, "sp002/03_verificacion_niveles.csv")
