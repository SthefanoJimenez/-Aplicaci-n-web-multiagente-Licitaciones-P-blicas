"""Marco normativo que cita el asistente de postulación (HU-014).

Una sola fuente para no repetir (ni contradecir) la norma en los textos. La Ley N.° 30225 y el
OSCE están derogados: desde el 22/04/2025 rige la Ley N.° 32069, Ley General de Contrataciones
Públicas, con su Reglamento aprobado por el Decreto Supremo N.° 009-2025-EF, y el OECE reemplazó
al OSCE (incluida la administración del RNP).
"""

LAW = "Ley N.° 32069, Ley General de Contrataciones Públicas"
LAW_SHORT = "Ley N.° 32069"
REGULATION = "Decreto Supremo N.° 009-2025-EF"
OECE = "OECE"
OECE_FULL = "Organismo Especializado para las Contrataciones Públicas Eficientes (OECE)"

LAW_URL = "https://www.gob.pe/institucion/oece/normas-legales"
RNP_URL = "https://www.gob.pe/rnp"
SEACE_URL = "https://prodapp2.seace.gob.pe/seacebus-uiwd-pub/buscadorConvocatorias/buscadorConvocatorias.xhtml"

# Texto de cierre de cada respuesta: deja claro con qué norma se evalúa y de dónde salen los datos.
CLOSING = (f"Evaluación referencial basada en la {LAW} y su Reglamento ({REGULATION}), con los datos "
           f"de convocatorias que publica el {OECE_FULL}. La decisión final y la revisión de las bases "
           f"corren por cuenta de la empresa.")


def sources():
    """Enlaces que el asistente ofrece al final de la respuesta."""
    return [{"label": "Ley N.° 32069 y normas del OECE", "url": LAW_URL},
            {"label": "Registro Nacional de Proveedores (RNP)", "url": RNP_URL},
            {"label": "Buscador de convocatorias del SEACE", "url": SEACE_URL}]
