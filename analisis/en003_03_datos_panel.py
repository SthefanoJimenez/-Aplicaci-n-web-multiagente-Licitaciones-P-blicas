"""EN-003 (paso 3) · Datos para el panel de analítica y el detalle de mercado de la app.

El awards.csv que usa la app solo trae año, departamento, precio unitario y
postores. El asesor pidió ver también quién compra (entidad y lugar), quién gana
(proveedor), por cuánto (monto y cantidad) y por qué tipo de proceso. Este script
toma la tabla analítica de EN-003 y le agrega esas columnas desde los Excel
originales de CONOSCE, unidas por la llave de SP-001 (convocatoria + ítem).

Requisitos: haber ejecutado en003_02_construir_dataset.py (deja
datos/procesados/tabla_analitica.csv, tabla_integrada.csv y salidas/en003/embudo.csv)
y tener los Excel en datos/crudos/ (o su copia en datos/cache/).

Uso, desde analisis/:
    python en003_03_datos_panel.py

Genera en datos/produccion/ (cópielos a app/data/):
    awards_detail.csv      una fila por adjudicación de bienes de la tabla analítica, con detalle
    cleaning_funnel.csv    pasos de limpieza de EN-003 y filas que quedan en cada uno
    data_quality.csv       indicadores de calidad por año (DO-002)
"""
import pandas as pd

from utils import ANIOS, BASE, PROCESADOS, cargar, llave

PRODUCCION = BASE / "datos" / "produccion"

# Columnas de los Excel de CONOSCE que se agregan (nombres verificados en 2023-2025)
DE_ADJUDICACIONES = {
    "entidad": "entidad",
    "tipoentidad": "tipo_entidad",
    "proceso": "proceso",
    "proveedor": "proveedor",
    "ruc_proveedor": "ruc_proveedor",
    "tipo_proveedor": "tipo_proveedor",
    "fecha_buenapro": "fecha_buenapro",
}
DE_CONVOCATORIAS = {
    "departamento_item": "departamento_entrega",
    "provincia_item": "provincia_entrega",
}


UNION = ["_cant", "_monto"]


def montos(tabla):
    """Cantidad y monto redondeados, para unir sin errores de decimales entre el Excel y el CSV."""
    return {"_cant": tabla["cantidad_adjudicado_item"].astype(float).round(4),
            "_monto": tabla["monto_adjudicado_item_soles"].astype(float).round(2)}


def persona_natural(tipo, ruc):
    """Los RUC 10 son personas naturales: su nombre no se muestra en la app."""
    return str(tipo).strip().lower() == "persona natural" or str(ruc).strip().startswith("10")


def main():
    analitica = pd.read_csv(PROCESADOS / "tabla_analitica.csv")
    print(f"Tabla analítica: {len(analitica):,} adjudicaciones de bienes")

    # Un mismo ítem (convocatoria + ítem) puede adjudicarse a varios proveedores, cada uno con su
    # cantidad y monto. Por eso la unión no usa solo la llave: también la cantidad, el monto y el
    # orden de aparición dentro de ese grupo. Así cada fila de la tabla analítica recibe a su propio
    # proveedor y ninguna adjudicación se repite ni se pierde.
    extras = []
    for anio in ANIOS:
        adj = cargar("ADJUDICACIONES", anio)
        con = cargar("CONVOCATORIAS", anio)
        a = adj.assign(_k=llave(adj, "ADJUDICACIONES"), **montos(adj))[["_k", *UNION, *DE_ADJUDICACIONES]]
        a = a.rename(columns=DE_ADJUDICACIONES).assign(anio=anio)
        c = con.assign(_k=llave(con, "CONVOCATORIAS"))[["_k", *DE_CONVOCATORIAS]].rename(columns=DE_CONVOCATORIAS)
        extras.append(a.merge(c.drop_duplicates("_k"), on="_k", how="left"))
    extras = pd.concat(extras, ignore_index=True)
    extras["_n"] = extras.groupby(["_k", "anio", *UNION]).cumcount()

    analitica["_k"] = analitica["codigoconvocatoria"].astype(str) + "|" + analitica["n_item"].astype(str)
    analitica = analitica.assign(**montos(analitica))
    analitica["_n"] = analitica.groupby(["_k", "anio", *UNION]).cumcount()
    d = analitica.merge(extras, on=["_k", "anio", *UNION, "_n"], how="left", validate="one_to_one")
    sin_par = d["proveedor"].isna() & d["entidad"].isna()
    print(f"Filas unidas con su adjudicación original: {len(d) - sin_par.sum():,} de {len(d):,}")
    repetidas = d.duplicated(["_k", "anio", "ruc_proveedor", "_cant", "_monto"]).sum()
    print(f"Filas repetidas tras la unión (deben ser 0 o casi 0): {repetidas:,}")

    natural = [persona_natural(t, r) for t, r in zip(d["tipo_proveedor"], d["ruc_proveedor"])]
    d["proveedor"] = [("Persona natural" if n else p) for n, p in zip(natural, d["proveedor"])]
    d["fecha_buenapro"] = pd.to_datetime(d["fecha_buenapro"], dayfirst=True, errors="coerce").dt.strftime("%Y-%m-%d")

    # Atípicos por bien (criterio de Tukey sobre el precio unitario): se marcan, no se borran
    q = d.groupby("codigoitem")["precio_unitario"].quantile([0.25, 0.75]).unstack()
    q.columns = ["_p25", "_p75"]
    d = d.join(q, on="codigoitem")
    iqr = d["_p75"] - d["_p25"]
    d["atipico"] = (d["precio_unitario"] < d["_p25"] - 1.5 * iqr) | (d["precio_unitario"] > d["_p75"] + 1.5 * iqr)

    salida = d.rename(columns={
        "tipoprocesoseleccion": "tipo_proceso", "cantidad_adjudicado_item": "cantidad",
        "monto_adjudicado_item_soles": "monto_adjudicado", "monto_referencial_item_soles": "monto_referencial",
    })[["codigoitem", "itemcubso", "anio", "codigoconvocatoria", "n_item", "proceso", "tipo_proceso", "unidad_medida",
        "cantidad", "monto_adjudicado", "monto_referencial", "precio_unitario", "n_postores",
        "entidad", "tipo_entidad", "entidad_departamento", "departamento_entrega", "provincia_entrega",
        "proveedor", "fecha_buenapro", "atipico"]]
    salida["n_postores"] = salida["n_postores"].astype("Int64")

    PRODUCCION.mkdir(parents=True, exist_ok=True)
    salida.to_csv(PRODUCCION / "awards_detail.csv", index=False, encoding="utf-8")

    # Embudo de EN-003
    pd.read_csv(BASE / "salidas" / "en003" / "embudo.csv").to_csv(
        PRODUCCION / "cleaning_funnel.csv", index=False, encoding="utf-8")

    # Calidad por año sobre la tabla integrada (indicadores de DO-002)
    integrada = pd.read_csv(PROCESADOS / "tabla_integrada.csv", usecols=["anio", "codigoitem", "n_postores", "objetocontractual"])
    calidad = integrada.groupby("anio").apply(lambda g: pd.Series({
        "adjudicaciones": len(g),
        "pct_bienes": round(100 * (g["objetocontractual"] == "Bien").mean(), 1),
        "pct_sin_cubso": round(100 * (g["codigoitem"].isna() | (g["codigoitem"] <= 0)).mean(), 1),
        "pct_sin_postores": round(100 * g["n_postores"].isna().mean(), 1),
    }), include_groups=False).reset_index()
    calidad.to_csv(PRODUCCION / "data_quality.csv", index=False, encoding="utf-8")

    print("\n### DATOS COMPLETOS EN awards_detail.csv")
    for col in ["entidad", "proveedor", "provincia_entrega", "fecha_buenapro", "tipo_proceso", "monto_adjudicado"]:
        print(f"  {col:<18} {100 * salida[col].notna().mean():5.1f}%")
    print(f"  personas naturales ocultas  {sum(natural):,}")
    print(f"  atípicos marcados           {int(salida['atipico'].sum()):,}")
    print(calidad.to_string(index=False))
    size = (PRODUCCION / "awards_detail.csv").stat().st_size / 1e6
    print(f"\nGuardado en datos/produccion/: awards_detail.csv ({len(salida):,} filas, {size:.1f} MB), "
          "cleaning_funnel.csv y data_quality.csv. Cópielos a app/data/.")


if __name__ == "__main__":
    main()