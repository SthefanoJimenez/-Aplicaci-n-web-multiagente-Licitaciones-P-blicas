"""Reglas del sistema definidas en SP-002.

Este módulo es la tabla de reglas en forma de código. Lo usan los scripts de
análisis y lo usará el agente evaluador (HU-005), para que ambos apliquen
exactamente las mismas reglas.
"""

# Mínimos de adjudicaciones
MIN_CATALOGO = 5      # para que un bien aparezca en el catálogo del clasificador
MIN_VEREDICTO = 10    # para emitir un veredicto
MIN_CONFIANZA_ALTA = 30   # para que el veredicto pueda tener confianza alta

# Cortes de dispersión: rango intercuartílico relativo = (P75 - P25) / mediana
DISPERSION_BAJA_HASTA = 0.25   # la mitad central varía hasta 25% del precio típico
DISPERSION_MEDIA_HASTA = 0.50  # la mitad central varía hasta 50% del precio típico


def dispersion(p25, p50, p75):
    """Rango intercuartílico relativo."""
    return (p75 - p25) / p50


def nivel_dispersion(riq):
    if riq <= DISPERSION_BAJA_HASTA:
        return "baja"
    if riq <= DISPERSION_MEDIA_HASTA:
        return "media"
    return "alta"


def resultado_evidencia(n):
    """Qué puede decir el sistema según el número de adjudicaciones."""
    if n < MIN_CATALOGO:
        return "Sin evidencia para evaluar"      # el bien no está en el catálogo
    if n < MIN_VEREDICTO:
        return "Sin evidencia suficiente"        # pocas compras: rango solo como referencia
    return "Con veredicto"


def nivel_confianza(n, riq):
    """Nivel de confianza de un veredicto. Solo aplica si n >= MIN_VEREDICTO."""
    disp = nivel_dispersion(riq)
    if n >= MIN_CONFIANZA_ALTA:
        return {"baja": "Alta", "media": "Media", "alta": "Baja"}[disp]
    return {"baja": "Media", "media": "Baja", "alta": "Baja"}[disp]


def veredicto(costo, p25, p75):
    """Veredicto según el costo del usuario. Los límites P25 y P75 son inclusivos."""
    if costo < p25:
        return "Participa"
    if costo <= p75:
        return "Participa con cautela"
    return "No rentable"
