import pandas as pd
from langsmith import traceable

import datos
import reglas

DEPARTAMENTO_LOCAL = "LA LIBERTAD"   # región de las empresas de Trujillo (alcance del proyecto)


# En LangSmith se registra cuántas adjudicaciones se usaron, no las listas completas
@traceable(run_type="tool", name="Filtrar adjudicaciones",
           process_outputs=lambda salida: {"n_adjudicaciones": len(salida["precios"])})
def filtrar(codigo, unidad):
    """Datos de las adjudicaciones del bien, en su unidad de medida.

    "postores" tiene un valor por adjudicación; None si no hay dato.
    """
    tabla = datos.ADJUDICACIONES
    filas = tabla[(tabla["codigoitem"] == codigo) & (tabla["unidad_medida"] == unidad)]
    return {
        "precios": filas["precio_unitario"].tolist(),
        "anios": filas["anio"].tolist(),
        "postores": [None if pd.isna(n) else int(n) for n in filas["n_postores"]],
        "departamentos": filas["entidad_departamento"].tolist(),
    }


@traceable(run_type="tool", name="Calcular rango de precios",
           process_inputs=lambda entradas: {"n_precios": len(entradas.get("precios", []))})
def calcular_rango(precios):
    """P25, mediana y P75 con interpolación lineal (método de SP-002)."""
    serie = pd.Series(precios, dtype="float64")
    p25, mediana, p75 = serie.quantile([0.25, 0.50, 0.75], interpolation="linear").tolist()
    return {"p25": p25, "mediana": mediana, "p75": p75}


@traceable(run_type="tool", name="Calcular competencia",
           process_inputs=lambda entradas: {"n_adjudicaciones": len(entradas.get("postores", []))})
def calcular_competencia(postores, departamentos):
    """Mediana de postores, % de procesos con un solo postor y adjudicaciones locales."""
    con_dato = pd.Series([n for n in postores if n is not None], dtype="float64")
    n_con_dato = len(con_dato)
    n_unico = int((con_dato == 1).sum())
    return {
        "n_con_postores": n_con_dato,
        "mediana_postores": float(con_dato.median()) if n_con_dato else None,
        "n_postor_unico": n_unico,
        "pct_postor_unico": 100 * n_unico / n_con_dato if n_con_dato else None,
        "n_la_libertad": sum(d == DEPARTAMENTO_LOCAL for d in departamentos),
    }


@traceable(run_type="chain", name="Agente analista de mercado")
def analizar(codigo):
    """Devuelve el rango de precios, la competencia y la evidencia en que se basan.

    Resultado: {"codigo", "nombre", "unidad", "n_adjudicaciones", "anio_desde",
                "anio_hasta", "p25", "mediana", "p75", "dispersion",
                "n_con_postores", "mediana_postores", "n_postor_unico", "pct_postor_unico",
                "n_la_libertad", "evidencia"}
    "evidencia" es uno de los resultados de reglas.resultado_evidencia().
    Si el bien no tiene adjudicaciones, los precios y el periodo son None.
    """
    bien = datos.BIENES.get(codigo)
    if bien is None:
        return _sin_datos(codigo, nombre=None, unidad=None)

    filtrado = filtrar(codigo, bien["unidad"])
    n = len(filtrado["precios"])
    if n == 0:
        return _sin_datos(codigo, bien["nombre"], bien["unidad"])

    rango = calcular_rango(filtrado["precios"])
    competencia = calcular_competencia(filtrado["postores"], filtrado["departamentos"])
    return {
        "codigo": codigo,
        "nombre": bien["nombre"],
        "unidad": bien["unidad"],
        "n_adjudicaciones": n,
        "anio_desde": int(min(filtrado["anios"])),
        "anio_hasta": int(max(filtrado["anios"])),
        **rango,
        # La dispersión la usará el evaluador para el nivel de confianza (HU-005)
        "dispersion": reglas.dispersion(rango["p25"], rango["mediana"], rango["p75"])
        if rango["mediana"] > 0 else None,
        **competencia,
        "evidencia": reglas.resultado_evidencia(n),
    }


def _sin_datos(codigo, nombre, unidad):
    return {
        "codigo": codigo, "nombre": nombre, "unidad": unidad, "n_adjudicaciones": 0,
        "anio_desde": None, "anio_hasta": None, "p25": None, "mediana": None, "p75": None,
        "dispersion": None, "n_con_postores": 0, "mediana_postores": None, "n_postor_unico": 0,
        "pct_postor_unico": None, "n_la_libertad": 0, "evidencia": reglas.resultado_evidencia(0),
    }