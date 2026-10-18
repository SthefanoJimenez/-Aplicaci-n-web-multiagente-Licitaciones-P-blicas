import hashlib

import pandas as pd

from utils import ANIOS, BASE, PROCESADOS, cargar, guardar, llave

MONEDA_NACIONAL = ["Soles", "Nuevos Soles"]
COLUMNAS = [
    "anio", "codigoconvocatoria", "n_item", "objetocontractual", "tipoprocesoseleccion",
    "moneda", "cantidad_adjudicado_item", "monto_adjudicado_item_soles",
    "monto_referencial_item_soles", "unidad_medida", "entidad_departamento",
    "codigoitem", "itemcubso", "n_postores",
]

partes, coincidencia = [], []
for anio in ANIOS:
    adj = cargar("ADJUDICACIONES", anio)
    con = cargar("CONVOCATORIAS", anio)
    pos = cargar("POSTOR", anio)

    adj = adj.assign(anio=anio, _k=llave(adj, "ADJUDICACIONES"))
    con = con.assign(_k=llave(con, "CONVOCATORIAS"))
    pos = pos.assign(_k=llave(pos, "POSTOR"))

    assert con["_k"].is_unique, f"Convocatorias {anio}: la llave debería ser única"
    cubso = con.set_index("_k")[["codigoitem", "itemcubso"]]
    # Postores distintos por ítem (un mismo RUC puede repetirse en un ítem)
    n_postores = pos.groupby("_k")["ruc_codigo_postor"].nunique().rename("n_postores")

    partes.append(adj.join(cubso, on="_k").join(n_postores, on="_k"))
    coincidencia.append({
        "anio": anio,
        "adjudicaciones": len(adj),
        "pct_con_convocatoria": round(100 * adj["_k"].isin(cubso.index).mean(), 1),
        "pct_con_postores": round(100 * adj["_k"].isin(n_postores.index).mean(), 1),
    })

integrada = pd.concat(partes, ignore_index=True)[COLUMNAS]
integrada["codigoitem"] = integrada["codigoitem"].astype("Int64")
integrada["n_postores"] = integrada["n_postores"].astype("Int64")
integrada = integrada.sort_values(
    ["anio", "codigoconvocatoria", "n_item"], kind="stable"
).reset_index(drop=True)

# Completitud: % de adjudicaciones con monto y cantidad válidos (métrica OE1)
derivable = (integrada["cantidad_adjudicado_item"] > 0) & (
    integrada["monto_adjudicado_item_soles"] > 0
)
completitud = round(100 * derivable.mean(), 1)

t = integrada
embudo = [("Tabla integrada", len(t))]
filtros = [
    ("Solo bienes", lambda d: d["objetocontractual"] == "Bien"),
    ("Solo moneda nacional", lambda d: d["moneda"].isin(MONEDA_NACIONAL)),
    # El OECE marca los ítems sin código con -1 ("NO DISPONIBLE"): no es un código real
    ("Solo con código CUBSO válido", lambda d: d["codigoitem"].notna() & (d["codigoitem"] > 0)),
    ("Sin contratación directa", lambda d: d["tipoprocesoseleccion"] != "Contratación Directa"),
    ("Monto y cantidad mayores que cero",
     lambda d: (d["cantidad_adjudicado_item"] > 0) & (d["monto_adjudicado_item_soles"] > 0)),
]
for nombre, condicion in filtros:
    t = t[condicion(t)]
    embudo.append((nombre, len(t)))

t = t.copy()
t["precio_unitario"] = t["monto_adjudicado_item_soles"] / t["cantidad_adjudicado_item"]

conteo = (
    t.groupby(["codigoitem", "unidad_medida"]).size().rename("n").reset_index()
    .sort_values(["codigoitem", "n", "unidad_medida"], ascending=[True, False, True])
)
mayoritaria = conteo.drop_duplicates("codigoitem")[["codigoitem", "unidad_medida"]]
unidades_por_codigo = conteo.groupby("codigoitem").size()
inconsistentes = conteo[conteo["codigoitem"].isin(unidades_por_codigo[unidades_por_codigo > 1].index)]
inconsistentes = inconsistentes.merge(
    mayoritaria.assign(se_conserva=True), on=["codigoitem", "unidad_medida"], how="left"
)
inconsistentes["se_conserva"] = inconsistentes["se_conserva"].notna()

t = t.merge(mayoritaria.assign(_ok=True), on=["codigoitem", "unidad_medida"], how="left")
t = t[t["_ok"].notna()].drop(columns="_ok").reset_index(drop=True)
embudo.append(("Solo la unidad mayoritaria de cada código", len(t)))

nombres = (
    t.groupby(["codigoitem", "anio", "itemcubso"]).size().rename("n").reset_index()
    .sort_values(["codigoitem", "anio", "n", "itemcubso"], ascending=[True, False, False, True])
    .drop_duplicates("codigoitem")[["codigoitem", "itemcubso"]]
)
t = t.drop(columns="itemcubso").merge(nombres, on="codigoitem", how="left")
analitica = t[COLUMNAS + ["precio_unitario"]]

grupos = (
    analitica.groupby(["codigoitem", "itemcubso", "unidad_medida"])
    .agg(n_adjudicaciones=("precio_unitario", "size"), anios_con_datos=("anio", "nunique"))
    .reset_index()
    .sort_values("codigoitem")
)

PROCESADOS.mkdir(parents=True, exist_ok=True)
tablas = {"tabla_integrada.csv": integrada, "tabla_analitica.csv": analitica, "grupos.csv": grupos}
ruta_huellas = BASE / "salidas" / "en003" / "huellas.csv"
anteriores = pd.read_csv(ruta_huellas) if ruta_huellas.exists() else None

huellas = []
for nombre, tabla in tablas.items():
    ruta = PROCESADOS / nombre
    tabla.to_csv(ruta, index=False, encoding="utf-8-sig")
    huellas.append({
        "archivo": nombre, "filas": len(tabla),
        "sha256": hashlib.sha256(ruta.read_bytes()).hexdigest(),
    })
huellas = pd.DataFrame(huellas)

resumen = pd.DataFrame([
    {"indicador": "Adjudicaciones en la tabla integrada", "valor": str(len(integrada))},
    {"indicador": "% de adjudicaciones con precio unitario derivable", "valor": f"{completitud}%"},
    {"indicador": "Filas en la tabla analítica", "valor": str(len(analitica))},
    {"indicador": "Códigos CUBSO en la tabla analítica", "valor": str(grupos["codigoitem"].nunique())},
    {"indicador": "Códigos con unidades inconsistentes", "valor": str(inconsistentes["codigoitem"].nunique())},
])

print("\n### COINCIDENCIA POR LA LLAVE DE CRUCE")
print(pd.DataFrame(coincidencia).to_string(index=False))
print("\n### EMBUDO DE LA TABLA ANALÍTICA")
print(pd.DataFrame(embudo, columns=["paso", "filas"]).to_string(index=False))
print("\n### RESUMEN")
print(resumen.to_string(index=False))

guardar(pd.DataFrame(coincidencia), "en003/coincidencia.csv")
guardar(pd.DataFrame(embudo, columns=["paso", "filas"]), "en003/embudo.csv")
guardar(inconsistentes, "en003/unidades_inconsistentes.csv")
guardar(resumen, "en003/resumen.csv")

print("\n### REPRODUCIBILIDAD")
if anteriores is None:
    print("  Primera ejecución: vuelva a ejecutar el script para comparar.")
else:
    iguales = anteriores.set_index("archivo")["sha256"].equals(huellas.set_index("archivo")["sha256"])
    print("  Resultados IDÉNTICOS a la ejecución anterior." if iguales
          else "  ATENCIÓN: los resultados DIFIEREN de la ejecución anterior.")
guardar(huellas, "en003/huellas.csv")
