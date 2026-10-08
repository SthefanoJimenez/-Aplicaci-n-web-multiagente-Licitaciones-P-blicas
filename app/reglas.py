

MIN_CATALOGO = 5      
MIN_VEREDICTO = 10    
MIN_CONFIANZA_ALTA = 30   

DISPERSION_BAJA_HASTA = 0.25   
DISPERSION_MEDIA_HASTA = 0.50  


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

    if n < MIN_CATALOGO:
        return "Sin evidencia para evaluar"      
    if n < MIN_VEREDICTO:
        return "Sin evidencia suficiente"        
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
