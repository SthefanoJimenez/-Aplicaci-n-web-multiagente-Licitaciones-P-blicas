import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import reglas as r
from utils import ANIOS, PROCESADOS, SALIDAS, guardar

integrada = pd.read_csv(PROCESADOS / "tabla_integrada.csv")
analitica = pd.read_csv(PROCESADOS / "tabla_analitica.csv")
inconsistentes = pd.read_csv(SALIDAS / "en003" / "unidades_inconsistentes.csv")
embudo = pd.read_csv(SALIDAS / "en003" / "embudo.csv")
DESTINO = SALIDAS / "do002"
DESTINO.mkdir(parents=True, exist_ok=True)
BANDAS = [0, 1, 2, 3, 5, 10, np.inf]
ETIQUETAS = ["1", "2", "3", "4-5", "6-10", "11+"]


def grafico(nombre):
    plt.tight_layout()
    plt.savefig(DESTINO / nombre, dpi=150)
    plt.close()
    print(f"  -> Gráfico guardado en salidas/do002/{nombre}")


b = analitica[(analitica["monto_referencial_item_soles"] > 0) & analitica["n_postores"].notna()].copy()
b["ratio"] = b["monto_adjudicado_item_soles"] / b["monto_referencial_item_soles"]
b["postores"] = pd.cut(b["n_postores"], BANDAS, labels=ETIQUETAS)
postores_precio = (
    b.groupby("postores", observed=True)
    .agg(adjudicaciones=("ratio", "size"), ratio_mediano=("ratio", "median"))
    .reset_index()
)
postores_precio["rebaja_mediana_pct"] = (100 * (1 - postores_precio["ratio_mediano"])).round(1)
postores_precio["ratio_mediano"] = postores_precio["ratio_mediano"].round(3)
correlacion = b[["n_postores", "ratio"]].corr(method="spearman").iloc[0, 1]

print("### 1. POSTORES Y PRECIO ADJUDICADO (bienes)")
print(postores_precio.to_string(index=False))
print(f"  Correlación de Spearman entre postores y ratio: {correlacion:.3f} "
      f"({len(b):,} adjudicaciones)")

plt.figure(figsize=(7, 4))
barras = plt.bar(postores_precio["postores"].astype(str), postores_precio["ratio_mediano"], color="#2f6f9f")
plt.bar_label(barras, labels=[f"{v:.3f}" for v in postores_precio["ratio_mediano"]], padding=3)
plt.axhline(1, color="gray", linestyle="--", linewidth=1)
plt.ylim(0.5, 1.1)
plt.xlabel("Número de postores")
plt.ylabel("Monto adjudicado / valor referencial (mediana)")
plt.title("A más postores, menor precio adjudicado (bienes, 2023-2025)")
grafico("fig1_postores_precio.png")

# ---------------------------------------------------------------------------
# 2. % de ítems con un solo postor, por año y en total
# ---------------------------------------------------------------------------
def postor_unico(tabla):
    items = tabla.drop_duplicates(["anio", "codigoconvocatoria", "n_item"])
    items = items[items["n_postores"].notna()]
    por_anio = items.groupby("anio")["n_postores"].apply(lambda s: 100 * (s == 1).mean())
    return [*por_anio.round(1).tolist(), round(100 * (items["n_postores"] == 1).mean(), 1)], len(items)


todos, n_todos = postor_unico(integrada)
bienes, n_bienes = postor_unico(analitica)
postor = pd.DataFrame({
    "periodo": [str(a) for a in ANIOS] + ["Total"],
    "todas_las_contrataciones_pct": todos,
    "bienes_analiticos_pct": bienes,
})
print("\n### 2. % DE ÍTEMS CON UN SOLO POSTOR")
print(postor.to_string(index=False))
print(f"  Ítems con postores registrados: {n_todos:,} en total y {n_bienes:,} bienes analíticos")

x = np.arange(len(postor))
plt.figure(figsize=(7, 4))
b1 = plt.bar(x - 0.2, postor["todas_las_contrataciones_pct"], 0.4, label="Todas las contrataciones", color="#9fb7c9")
b2 = plt.bar(x + 0.2, postor["bienes_analiticos_pct"], 0.4, label="Bienes (tabla analítica)", color="#2f6f9f")
plt.bar_label(b1, fmt="%.1f%%", padding=2)
plt.bar_label(b2, fmt="%.1f%%", padding=2)
plt.xticks(x, postor["periodo"])
plt.ylim(0, 50)
plt.ylabel("% de ítems con un solo postor")
plt.title("Ítems adjudicados con un solo postor")
plt.legend()
grafico("fig2_postor_unico.png")

sin_cubso = integrada["codigoitem"].isna() | (integrada["codigoitem"] <= 0)
calidad = pd.DataFrame({
    "anio": ANIOS,
    "adjudicaciones": [int((integrada["anio"] == a).sum()) for a in ANIOS],
    "sin_cubso_pct": [round(100 * sin_cubso[integrada["anio"] == a].mean(), 1) for a in ANIOS],
    "codigo_menos_1": [int(((integrada["anio"] == a) & (integrada["codigoitem"] == -1)).sum()) for a in ANIOS],
    "sin_postores_pct": [round(100 * integrada.loc[integrada["anio"] == a, "n_postores"].isna().mean(), 1)
                         for a in ANIOS],
})
codigos_inconsistentes = inconsistentes["codigoitem"].nunique()
filas_descartadas = int(inconsistentes.loc[~inconsistentes["se_conserva"], "n"].sum())

# Justificación de excluir contrataciones directas (bienes en soles con código válido)
base = integrada[(integrada["objetocontractual"] == "Bien")
                 & integrada["moneda"].isin(["Soles", "Nuevos Soles"])
                 & (integrada["codigoitem"] > 0)
                 & (integrada["monto_referencial_item_soles"] > 0)].copy()
base["ratio"] = base["monto_adjudicado_item_soles"] / base["monto_referencial_item_soles"]
base["tipo"] = np.where(base["tipoprocesoseleccion"] == "Contratación Directa",
                        "Contratación directa", "Demás procedimientos")
directas = base.groupby("tipo").agg(
    adjudicaciones=("ratio", "size"),
    ratio_mediano=("ratio", "median"),
    postores_medianos=("n_postores", "median"),
    postor_unico_pct=("n_postores", lambda s: 100 * (s == 1).mean()),
).round(3).reset_index()

print("\n### 3. CALIDAD DE LOS DATOS")
print(calidad.to_string(index=False))
print(f"  Códigos con unidades inconsistentes: {codigos_inconsistentes} "
      f"(filas descartadas: {filas_descartadas:,})")
print("\n  Contrataciones directas frente a los demás procedimientos:")
print(directas.to_string(index=False))

plt.figure(figsize=(8, 4))
etapas = embudo.iloc[::-1]
barras = plt.barh(etapas["paso"], etapas["filas"], color="#2f6f9f")
plt.bar_label(barras, labels=[f"{v:,}" for v in etapas["filas"]], padding=3)
plt.xlim(0, embudo["filas"].max() * 1.2)
plt.xlabel("Adjudicaciones")
plt.title("De los datos crudos a la tabla analítica")
grafico("fig3_embudo.png")

st = analitica.groupby(["codigoitem", "itemcubso", "unidad_medida"])["precio_unitario"].agg(
    adjudicaciones="size",
    p25=lambda s: s.quantile(0.25),
    p50="median",
    p75=lambda s: s.quantile(0.75),
).reset_index()
st = st[st["adjudicaciones"] >= r.MIN_VEREDICTO].copy()
st["dispersion"] = r.dispersion(st["p25"], st["p50"], st["p75"]).round(3)
st["p50"] = st["p50"].round(2)
st["nivel"] = st["dispersion"].apply(r.nivel_dispersion)
cols = ["itemcubso", "unidad_medida", "adjudicaciones", "p50", "dispersion"]
menor = st.sort_values(["dispersion", "codigoitem"]).head(10)[cols]
mayor = st.sort_values(["dispersion", "codigoitem"], ascending=[False, True]).head(10)[cols]
niveles = st["nivel"].value_counts().reindex(["baja", "media", "alta"]).rename("bienes").reset_index()

print(f"\n### 4. DISPERSIÓN ({len(st)} bienes con {r.MIN_VEREDICTO} o más adjudicaciones)")
print(niveles.to_string(index=False))
print("\n  Menor dispersión:")
print(menor.to_string(index=False))
print("\n  Mayor dispersión:")
print(mayor.to_string(index=False))

plt.figure(figsize=(7, 4))
plt.hist(st["dispersion"].clip(upper=3), bins=30, color="#2f6f9f")
plt.axvline(r.DISPERSION_BAJA_HASTA, color="green", linestyle="--", label="Límite dispersión baja (0.25)")
plt.axvline(r.DISPERSION_MEDIA_HASTA, color="orange", linestyle="--", label="Límite dispersión media (0.50)")
plt.xlabel("Rango intercuartílico relativo (valores mayores a 3 se agrupan en 3)")
plt.ylabel("Bienes")
plt.title("Dispersión de precios por bien")
plt.legend()
grafico("fig4_dispersion.png")

guardar(postores_precio, "do002/01_postores_precio.csv")
guardar(postor, "do002/02_postor_unico.csv")
guardar(calidad, "do002/03_calidad_datos.csv")
guardar(directas, "do002/03_contrataciones_directas.csv")
guardar(niveles, "do002/04_niveles_dispersion.csv")
guardar(menor, "do002/04_menor_dispersion.csv")
guardar(mayor, "do002/04_mayor_dispersion.csv")
