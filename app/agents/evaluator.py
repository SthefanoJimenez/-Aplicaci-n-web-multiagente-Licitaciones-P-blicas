"""Agente evaluador (HU-005, OE4): cruza el costo del usuario con el rango de precios.

Es determinista: aplica la tabla de reglas de SP-002 (rules.py). Por privacidad, el
costo no se guarda, no se envía al modelo de lenguaje y no se registra en LangSmith:
por eso este módulo no usa @traceable ni pasa por un grafo trazado.
"""
import re

import rules

TONES = {rules.PARTICIPATE: "good", rules.PARTICIPATE_WITH_CAUTION: "caution", rules.NOT_PROFITABLE: "bad"}
CONFIDENCE_DOTS = {"high": 3, "medium": 2, "low": 1}
MAX_COST = 1_000_000_000


class InvalidCost(ValueError):
    """El costo está vacío, no es un número o no es positivo."""


def parse_cost(text):
    """Convierte lo que escribió el usuario en un número: acepta 30.5, 30,50 y 1,234.50."""
    value = (text or "").strip().replace("S/", "").replace(" ", "")
    if not value:
        raise InvalidCost("Escriba su costo por unidad para compararlo con el mercado.")
    if re.fullmatch(r"\d{1,3}(,\d{3})+\.\d+", value):          # 1,234.50 (coma de miles y punto decimal)
        value = value.replace(",", "")
    elif re.fullmatch(r"\d+,\d+", value):                       # 30,50 o 12,503: una sola coma es decimal
        value = value.replace(",", ".")
    try:
        cost = float(value)
    except ValueError:
        raise InvalidCost("Escriba solo el número, por ejemplo 28.50.") from None
    if not (0 < cost < MAX_COST):
        raise InvalidCost("El costo debe ser un número mayor que cero.")
    return cost


def _marker_position(cost, p25, p75):
    """Posición del costo en la barra de zonas (25% | 50% | 25%), en porcentaje del ancho."""
    width = (p75 - p25) or max(p75, 1e-9)
    if cost < p25:
        low = max(p25 - width, 0)
        share = (cost - low) / (p25 - low) if p25 > low else 1
        pct = 25 * share
    elif cost <= p75:
        pct = 25 + 50 * ((cost - p25) / (p75 - p25) if p75 > p25 else 0.5)
    else:
        pct = 75 + 25 * min((cost - p75) / width, 1)
    return min(max(pct, 2), 98)


def evaluate(cost, market):
    """Veredicto y nivel de confianza para un costo unitario.

    market: resultado de agents.market_analyst.analyze() con evidencia suficiente.
    """
    result = rules.verdict(cost, market["p25"], market["p75"])
    confidence = rules.confidence_level(market["n_awards"], market["dispersion"] or 0)
    return {
        "cost": cost,
        "verdict": result,
        "label": rules.VERDICT_LABELS[result],
        "tone": TONES[result],
        "confidence": confidence,
        "confidence_label": rules.CONFIDENCE_LABELS[confidence],
        "confidence_dots": CONFIDENCE_DOTS[confidence],
        "dispersion_level": rules.dispersion_level(market["dispersion"] or 0),
        "vs_median_pct": 100 * (cost - market["median"]) / market["median"] if market["median"] else None,
        "marker_pct": _marker_position(cost, market["p25"], market["p75"]),
    }