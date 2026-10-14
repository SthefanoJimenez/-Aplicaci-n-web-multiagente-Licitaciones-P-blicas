from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph

import config
import rules
from agents import classifier, market_analyst


class InvalidDescription(ValueError):
    """La descripción está vacía o es demasiado larga."""


NO_EVIDENCE = "no_evidence"                    
CANDIDATES = "candidates"
RANGE_WITH_EVIDENCE = "range_with_evidence"   
RANGE_FOR_REFERENCE = "range_for_reference"   


class IdentificationState(TypedDict, total=False):
    """Información que comparten los nodos del tramo 1."""
    description: str
    rephrased_description: Optional[str]
    candidates: list
    result: str


def classifier_node(state: IdentificationState) -> dict:
    """Agente clasificador: de la descripción a hasta 3 bienes del catálogo."""
    response = classifier.classify(state["description"])
    return {
        "candidates": response["candidates"],
        "rephrased_description": response["rephrased_description"],
    }


def has_candidates(state: IdentificationState) -> str:
    """Arista condicional: ¿quedó algún candidato válido?"""
    return "with_candidates" if state.get("candidates") else "without_candidates"


def show_candidates_node(state: IdentificationState) -> dict:
    return {"result": CANDIDATES}


def no_evidence_node(state: IdentificationState) -> dict:
    """Salida temprana: el pipeline termina sin llamar a los demás agentes."""
    return {"result": NO_EVIDENCE, "candidates": []}


def build_identification_stage():
    graph = StateGraph(IdentificationState)
    graph.add_node("classifier_agent", classifier_node)
    graph.add_node("show_candidates", show_candidates_node)
    graph.add_node("no_evidence", no_evidence_node)

    graph.add_edge(START, "classifier_agent")
    graph.add_conditional_edges(
        "classifier_agent",
        has_candidates,
        {"with_candidates": "show_candidates", "without_candidates": "no_evidence"},
    )
    graph.add_edge("show_candidates", END)
    graph.add_edge("no_evidence", END)
    return graph.compile(name="Stage 1: identify the good")


IDENTIFICATION_STAGE = build_identification_stage()


def identify_good(description):
    """Tramo 1 del pipeline: de la descripción del usuario a los candidatos del catálogo.

    Devuelve {"result": CANDIDATES o NO_EVIDENCE, "candidates": [...]}.
    Lanza InvalidDescription o llm.ModelError.
    """
    description = (description or "").strip()
    if not description:
        raise InvalidDescription("Escriba una descripción del bien para buscarlo en el catálogo.")
    if len(description) > config.MAX_DESCRIPTION_LENGTH:
        raise InvalidDescription(
            f"La descripción puede tener hasta {config.MAX_DESCRIPTION_LENGTH} caracteres. "
            "Describa solo el bien: qué es, su material y su medida o presentación."
        )

    state = IDENTIFICATION_STAGE.invoke(
        {"description": description},
        config={"run_name": "Identify the good", "metadata": {"model": config.MODEL}},
    )
    return {"result": state["result"], "candidates": state.get("candidates", [])}


class MarketState(TypedDict, total=False):
    """Información que comparten los nodos del tramo 2."""
    code: int
    market: dict
    result: str


def market_analyst_node(state: MarketState) -> dict:
    """Agente analista de mercado: rango de precios y competencia del bien (determinista)."""
    return {"market": market_analyst.analyze(state["code"])}


def evidence_level(state: MarketState) -> str:
    """Arista condicional: qué puede mostrar el sistema según el mínimo de SP-002."""
    evidence = state["market"]["evidence"]
    if evidence == rules.WITH_VERDICT:
        return "sufficient"
    if evidence == rules.INSUFFICIENT_EVIDENCE:
        return "insufficient"
    return "no_evidence"


def show_range_node(state: MarketState) -> dict:
    return {"result": RANGE_WITH_EVIDENCE}


def reference_range_node(state: MarketState) -> dict:
    """Pocas adjudicaciones: se muestra el rango como referencia, sin pedir el costo."""
    return {"result": RANGE_FOR_REFERENCE}


def market_no_evidence_node(state: MarketState) -> dict:
    return {"result": NO_EVIDENCE}


def build_market_stage():
    graph = StateGraph(MarketState)
    graph.add_node("market_analyst_agent", market_analyst_node)
    graph.add_node("show_range", show_range_node)
    graph.add_node("reference_range", reference_range_node)
    graph.add_node("no_evidence", market_no_evidence_node)

    graph.add_edge(START, "market_analyst_agent")
    graph.add_conditional_edges(
        "market_analyst_agent",
        evidence_level,
        {
            "sufficient": "show_range",
            "insufficient": "reference_range",
            "no_evidence": "no_evidence",
        },
    )
    for node in ("show_range", "reference_range", "no_evidence"):
        graph.add_edge(node, END)
    return graph.compile(name="Stage 2: analyze the market")


MARKET_STAGE = build_market_stage()


def analyze_market(code):
    """Tramo 2 del pipeline: del código confirmado al rango de precios y la competencia.

    Devuelve {"result": RANGE_WITH_EVIDENCE, RANGE_FOR_REFERENCE o NO_EVIDENCE,
              "market": {...}}.
    No usa el modelo de lenguaje.
    """
    state = MARKET_STAGE.invoke(
        {"code": int(code)},
        config={"run_name": "Analyze the market"},
    )
    return {"result": state["result"], "market": state["market"]}