import os

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "LicitaFácil"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY", "solo-para-desarrollo-local")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
# Modelo de respaldo opcional: se usa solo si el principal sigue fallando tras los reintentos
# (por ejemplo, error 503 de saturación). Vacío = sin respaldo.
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "").strip() or None

RETRIES = 2
RETRY_WAIT_SECONDS = 2

MAX_DESCRIPTION_LENGTH = 300

# Cadena de conexión de PostgreSQL (Neon). Solo en variables de entorno de Render y secretos
# de GitHub, nunca en el repositorio. Vacía = la app funciona sin convocatorias (aviso RN-002).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None

# Nombre de cliente para la API OCDS. Por defecto, el de un navegador: desde el 28/10/2026 el
# portal responde 403 a "LicitaFacil-SP005" (SP-006). Decisión del PO, registrada como riesgo.
OCDS_USER_AGENT = os.environ.get("OCDS_USER_AGENT", "").strip() or (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Meses de archivos que lee la ingesta, contando el actual (SP-006 propuso 2; se prueba con 2).
OCDS_MONTHS = int(os.environ.get("OCDS_MONTHS", "").strip() or "2")

# Rubros piloto (familia UNSPSC): 1510 combustibles y 2510 vehículos.
PILOT_RUBROS = tuple(r.strip() for r in os.environ.get("PILOT_RUBROS", "1510,2510").split(",") if r.strip())