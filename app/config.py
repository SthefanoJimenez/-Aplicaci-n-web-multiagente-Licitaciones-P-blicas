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

# HU-008: opcional para probar cuentas en una base local sin escribir en Neon.
# Vacía = comparte DATABASE_URL; si tampoco existe, usa app/instance/cuentas.sqlite3.
ACCOUNTS_DATABASE_URL = os.environ.get("ACCOUNTS_DATABASE_URL", "").strip() or None

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
# RUBROS_INGESTA=todos: la ingesta guarda los bienes vigentes de TODOS los rubros que trae la API. Sin ella,
# solo los piloto y los solicitados (EN-011). La lectura de bases atiende primero a piloto y solicitados.
ALL_RUBROS = os.environ.get("RUBROS_INGESTA", "").strip().lower() == "todos"

# RAG de las bases (EN-012). Modelo de embeddings de Gemini y tamaño del vector: 768 es una de las
# dimensiones que recomienda Google para gemini-embedding-001 (por defecto entrega 3072).
EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "").strip() or "gemini-embedding-001"
EMBEDDING_DIM = 768
# Bases que se descargan e indexan por ejecución de la ingesta (cada una tarda segundos, o minutos con OCR)
RAG_MAX_BASES_PER_RUN = int(os.environ.get("RAG_MAX_BASES_PER_RUN", "").strip() or "25")
# Tamaño máximo de las bases que descarga la lectura (la pantalla de descarga mantiene 80 MB).
# El 03/11/2026, 2 de 142 bases vigentes pasaban de 80 MB.
RAG_MAX_BASES_MB = int(os.environ.get("RAG_MAX_BASES_MB", "").strip() or "250")
