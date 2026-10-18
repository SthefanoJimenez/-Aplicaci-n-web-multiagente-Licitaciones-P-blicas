import pandas as pd

from utils import ANIOS, FUENTES, cargar, guardar, llave

unicidad, cruces, cardinalidad = [], [], []
codigos_por_anio = {}

for anio in ANIOS:
    df = {f: cargar(f, anio) for f in FUENTES}
    k = {f: llave(df[f], f) for f in FUENTES}

    for f in FUENTES:
        unicidad.append({
            "anio": anio, "fuente": f, "filas": len(k[f]),
            "llaves_unicas": k[f].nunique(),
            "filas_repetidas": len(k[f]) - k[f].nunique(),
        })

    def pct(a, b):
        return round(100 * k[a].isin(set(k[b])).mean(), 1)

    cruces.append({
        "anio": anio,
        "ADJ->CON": pct("ADJUDICACIONES", "CONVOCATORIAS"),
        "CON->ADJ": pct("CONVOCATORIAS", "ADJUDICACIONES"),
        "ADJ->POS": pct("ADJUDICACIONES", "POSTOR"),
        "POS->ADJ": pct("POSTOR", "ADJUDICACIONES"),
    })

    postores = k["POSTOR"].value_counts()
    adj = df["ADJUDICACIONES"].assign(_k=k["ADJUDICACIONES"])
    repetidos = adj[adj["_k"].duplicated(keep=False)]
    proveedores = repetidos.groupby("_k")["ruc_proveedor"].nunique()
    cardinalidad.append({
        "anio": anio,
        "postores_por_item_min": int(postores.min()),
        "postores_por_item_mediana": float(postores.median()),
        "postores_por_item_max": int(postores.max()),
        "items_con_varias_adjudicaciones": len(proveedores),
        "de_ellos_con_proveedores_distintos": int((proveedores > 1).sum()),
    })

    codigos_por_anio[anio] = set(df["CONVOCATORIAS"]["codigoconvocatoria"])

repetidos_entre_anios = {
    "2023 y 2024": len(codigos_por_anio[2023] & codigos_por_anio[2024]),
    "2024 y 2025": len(codigos_por_anio[2024] & codigos_por_anio[2025]),
    "2023 y 2025": len(codigos_por_anio[2023] & codigos_por_anio[2025]),
}

tablas = {
    "UNICIDAD DE LA LLAVE": pd.DataFrame(unicidad),
    "COINCIDENCIA ENTRE ARCHIVOS (%)": pd.DataFrame(cruces),
    "CARDINALIDAD": pd.DataFrame(cardinalidad),
}
for titulo, tabla in tablas.items():
    print(f"\n### {titulo}")
    print(tabla.to_string(index=False))

print("\n### CODIGOS DE CONVOCATORIA REPETIDOS ENTRE AÑOS")
for par, n in repetidos_entre_anios.items():
    print(f"  {par}: {n}")

guardar(tablas["UNICIDAD DE LA LLAVE"], "sp001/02_unicidad_llave.csv")
guardar(tablas["COINCIDENCIA ENTRE ARCHIVOS (%)"], "sp001/02_coincidencia_archivos.csv")
guardar(tablas["CARDINALIDAD"], "sp001/02_cardinalidad.csv")
