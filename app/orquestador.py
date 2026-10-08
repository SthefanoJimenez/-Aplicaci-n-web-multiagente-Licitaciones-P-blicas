from typing import Optional
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph

import config
from agentes import clasificador


class DescripcionInvalida(ValueError):
    """La descripción está vacía o es demasiado larga."""


SIN_EVIDENCIA_PARA_EVALUAR = "sin_evidencia_para_evaluar"
CANDIDATOS = "candidatos"


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
    grafo = StateGraph(EstadoIdentificacion)  # type: ignore
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