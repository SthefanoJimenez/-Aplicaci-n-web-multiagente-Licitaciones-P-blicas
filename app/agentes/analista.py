import pandas as pd
from langsmith import traceable

import datos
import reglas

@traceable(run_type="tool", name="Filtrar adjudicaciones",
           process_outputs=lambda salida: {"n_adjudicaciones": len(salida["precios"])})
def filtrar(codigo, unidad):
    """Precios unitarios y años de las adjudicaciones del bien, en su unidad de medida."""
    tabla = datos.ADJUDICACIONES
    filas = tabla[(tabla["codigoitem"] == codigo) & (tabla["unidad_medida"] == unidad)]
    return {
        "precios": filas["precio_unitario"].tolist(),
        "anios": filas["anio"].tolist(),
    }


@traceable(run_type="tool", name="Calcular rango de precios",
           process_inputs=lambda entradas: {"n_precios": len(entradas.get("precios", []))})
def calcular_rango(precios):
    """P25, mediana y P75 con interpolación lineal (método de SP-002)."""
    serie = pd.Series(precios, dtype="float64")
    p25, mediana, p75 = serie.quantile([0.25, 0.50, 0.75], interpolation="linear").tolist()
    return {"p25": p25, "mediana": mediana, "p75": p75}


@traceable(run_type="chain", name="Agente analista de mercado")
def analizar(codigo):
    """Devuelve el rango de precios del bien y la evidencia en que se basa.

    Resultado: {"codigo", "nombre", "unidad", "n_adjudicaciones", "anio_desde",
                "anio_hasta", "p25", "mediana", "p75", "dispersion", "evidencia"}
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
    return {
        "codigo": codigo,
        "nombre": bien["nombre"],
        "unidad": bien["unidad"],
        "n_adjudicaciones": n,
        "anio_desde": int(min(filtrado["anios"])),
        "anio_hasta": int(max(filtrado["anios"])),
        **rango,
        "dispersion": reglas.dispersion(rango["p25"], rango["mediana"], rango["p75"])
        if rango["mediana"] > 0 else None,
        "evidencia": reglas.resultado_evidencia(n),
    }


def _sin_datos(codigo, nombre, unidad):
    return {
        "codigo": codigo, "nombre": nombre, "unidad": unidad, "n_adjudicaciones": 0,
        "anio_desde": None, "anio_hasta": None, "p25": None, "mediana": None, "p75": None,
        "dispersion": None, "evidencia": reglas.resultado_evidencia(0),
    }