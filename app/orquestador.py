from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph

import config
import reglas
from agentes import analista, clasificador


class DescripcionInvalida(ValueError):
    """La descripción está vacía o es demasiado larga."""


# Resultados posibles de los tramos
SIN_EVIDENCIA_PARA_EVALUAR = "sin_evidencia_para_evaluar"
CANDIDATOS = "candidatos"
RANGO_CON_EVIDENCIA = "rango_con_evidencia"            # 10 o más adjudicaciones
RANGO_REFERENCIAL = "sin_evidencia_suficiente"         # de 5 a 9: rango solo como referencia


# ---------------------------------------------------------------------------
# Tramo 1: identificar el bien
# ---------------------------------------------------------------------------
class EstadoIdentificacion(TypedDict, total=False):
    """Información que comparten los nodos del tramo 1."""
    descripcion: str
    descripcion_reformulada: Optional[str]
    candidatos: list
    resultado: str


def nodo_clasificador(estado: EstadoIdentificacion) -> dict:
    """Agente clasificador: de la descripción a hasta 3 bienes del catálogo."""
    respuesta = clasificador.clasificar(estado["descripcion"])
    return {
        "candidatos": respuesta["candidatos"],
        "descripcion_reformulada": respuesta["descripcion_reformulada"],
    }


def hay_candidatos(estado: EstadoIdentificacion) -> str:
    """Arista condicional: ¿quedó algún candidato válido?"""
    return "con_candidatos" if estado.get("candidatos") else "sin_candidatos"


def nodo_mostrar_candidatos(estado: EstadoIdentificacion) -> dict:
    return {"resultado": CANDIDATOS}


def nodo_sin_evidencia(estado: EstadoIdentificacion) -> dict:
    """Salida temprana: el pipeline termina sin llamar a los demás agentes."""
    return {"resultado": SIN_EVIDENCIA_PARA_EVALUAR, "candidatos": []}


def construir_tramo_identificacion():
    grafo = StateGraph(EstadoIdentificacion)
    grafo.add_node("agente_clasificador", nodo_clasificador)
    grafo.add_node("mostrar_candidatos", nodo_mostrar_candidatos)
    grafo.add_node("sin_evidencia_para_evaluar", nodo_sin_evidencia)

    grafo.add_edge(START, "agente_clasificador")
    grafo.add_conditional_edges(
        "agente_clasificador",
        hay_candidatos,
        {"con_candidatos": "mostrar_candidatos", "sin_candidatos": "sin_evidencia_para_evaluar"},
    )
    grafo.add_edge("mostrar_candidatos", END)
    grafo.add_edge("sin_evidencia_para_evaluar", END)
    return grafo.compile(name="Tramo 1: identificar el bien")


TRAMO_IDENTIFICACION = construir_tramo_identificacion()


def identificar_bien(descripcion):
    """Tramo 1 del pipeline: de la descripción del usuario a los candidatos del catálogo.

    Devuelve {"resultado": CANDIDATOS o SIN_EVIDENCIA_PARA_EVALUAR, "candidatos": [...]}.
    Lanza DescripcionInvalida o llm.ErrorModelo.
    """
    descripcion = (descripcion or "").strip()
    if not descripcion:
        raise DescripcionInvalida("Escriba una descripción del bien para buscarlo en el catálogo.")
    if len(descripcion) > config.LARGO_MAXIMO_DESCRIPCION:
        raise DescripcionInvalida(
            f"La descripción puede tener hasta {config.LARGO_MAXIMO_DESCRIPCION} caracteres. "
            "Describa solo el bien: qué es, su material y su medida o presentación."
        )

    estado = TRAMO_IDENTIFICACION.invoke(
        {"descripcion": descripcion},
        config={"run_name": "Identificar el bien", "metadata": {"modelo": config.MODELO}},
    )
    return {"resultado": estado["resultado"], "candidatos": estado.get("candidatos", [])}


# ---------------------------------------------------------------------------
# Tramo 2: analizar el mercado
# ---------------------------------------------------------------------------
class EstadoMercado(TypedDict, total=False):
    """Información que comparten los nodos del tramo 2."""
    codigo: int
    mercado: dict
    resultado: str


def nodo_analista(estado: EstadoMercado) -> dict:
    """Agente analista de mercado: rango de precios del bien (determinista)."""
    return {"mercado": analista.analizar(estado["codigo"])}


def nivel_de_evidencia(estado: EstadoMercado) -> str:
    """Arista condicional: qué puede mostrar el sistema según el mínimo de SP-002."""
    evidencia = estado["mercado"]["evidencia"]
    if evidencia == reglas.resultado_evidencia(reglas.MIN_VEREDICTO):
        return "suficiente"
    if evidencia == reglas.resultado_evidencia(reglas.MIN_CATALOGO):
        return "insuficiente"
    return "sin_evidencia"


def nodo_mostrar_rango(estado: EstadoMercado) -> dict:
    return {"resultado": RANGO_CON_EVIDENCIA}


def nodo_rango_referencial(estado: EstadoMercado) -> dict:
    """Pocas adjudicaciones: se muestra el rango como referencia, sin pedir el costo."""
    return {"resultado": RANGO_REFERENCIAL}


def nodo_mercado_sin_evidencia(estado: EstadoMercado) -> dict:
    return {"resultado": SIN_EVIDENCIA_PARA_EVALUAR}


def construir_tramo_mercado():
    grafo = StateGraph(EstadoMercado)
    grafo.add_node("agente_analista", nodo_analista)
    grafo.add_node("mostrar_rango", nodo_mostrar_rango)
    grafo.add_node("rango_referencial", nodo_rango_referencial)
    grafo.add_node("sin_evidencia_para_evaluar", nodo_mercado_sin_evidencia)

    grafo.add_edge(START, "agente_analista")
    grafo.add_conditional_edges(
        "agente_analista",
        nivel_de_evidencia,
        {
            "suficiente": "mostrar_rango",
            "insuficiente": "rango_referencial",
            "sin_evidencia": "sin_evidencia_para_evaluar",
        },
    )
    for nodo in ("mostrar_rango", "rango_referencial", "sin_evidencia_para_evaluar"):
        grafo.add_edge(nodo, END)
    return grafo.compile(name="Tramo 2: analizar el mercado")


TRAMO_MERCADO = construir_tramo_mercado()


def analizar_mercado(codigo):
    """Tramo 2 del pipeline: del código confirmado al rango de precios.

    Devuelve {"resultado": RANGO_CON_EVIDENCIA, RANGO_REFERENCIAL o
              SIN_EVIDENCIA_PARA_EVALUAR, "mercado": {...}}.
    No usa el modelo de lenguaje.
    """
    estado = TRAMO_MERCADO.invoke(
        {"codigo": int(codigo)},
        config={"run_name": "Analizar el mercado"},
    )
    return {"resultado": estado["resultado"], "mercado": estado["mercado"]}