
MIN_CATALOG = 5           
MIN_VERDICT = 10          
MIN_HIGH_CONFIDENCE = 30  

LOW_DISPERSION_MAX = 0.25     
MEDIUM_DISPERSION_MAX = 0.50  
NO_EVIDENCE = "no_evidence"                    
INSUFFICIENT_EVIDENCE = "insufficient_evidence" 
WITH_VERDICT = "with_verdict"                  

EVIDENCE_LABELS = {
    NO_EVIDENCE: "Sin evidencia para evaluar",
    INSUFFICIENT_EVIDENCE: "Sin evidencia suficiente",
    WITH_VERDICT: "Con veredicto",
}


PARTICIPATE = "participate"
PARTICIPATE_WITH_CAUTION = "participate_with_caution"
NOT_PROFITABLE = "not_profitable"

VERDICT_LABELS = {
    PARTICIPATE: "Participa",
    PARTICIPATE_WITH_CAUTION: "Participa con cautela",
    NOT_PROFITABLE: "No rentable",
}
CONFIDENCE_LABELS = {"high": "Alta", "medium": "Media", "low": "Baja"}


def dispersion(p25, p50, p75):
    """Rango intercuartílico relativo."""
    return (p75 - p25) / p50


def dispersion_level(iqr):
    if iqr <= LOW_DISPERSION_MAX:
        return "low"
    if iqr <= MEDIUM_DISPERSION_MAX:
        return "medium"
    return "high"


def evidence_result(n):
    """Qué puede decir el sistema según el número de adjudicaciones."""
    if n < MIN_CATALOG:
        return NO_EVIDENCE
    if n < MIN_VERDICT:
        return INSUFFICIENT_EVIDENCE
    return WITH_VERDICT


def confidence_level(n, iqr):
    """Nivel de confianza de un veredicto. Solo aplica si n >= MIN_VERDICT."""
    level = dispersion_level(iqr)
    if n >= MIN_HIGH_CONFIDENCE:
        return {"low": "high", "medium": "medium", "high": "low"}[level]
    return {"low": "medium", "medium": "low", "high": "low"}[level]


def verdict(cost, p25, p75):
    """Veredicto según el costo del usuario. Los límites P25 y P75 son inclusivos."""
    if cost < p25:
        return PARTICIPATE
    if cost <= p75:
        return PARTICIPATE_WITH_CAUTION
    return NOT_PROFITABLE