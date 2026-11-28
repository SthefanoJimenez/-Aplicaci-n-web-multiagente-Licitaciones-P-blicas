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

# Autocompletado del perfil: sin token se conserva el ingreso manual.
MIAPI_RUC_TOKEN = os.environ.get("MIAPI_RUC_TOKEN", "").strip()
MIAPI_RUC_DAILY_LIMIT = os.environ.get("MIAPI_RUC_DAILY_LIMIT", "20")

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
# RUBROS_INGESTA: qué rubros (familias UNSPSC) guarda la ingesta, dentro de las categorías de CATEGORIAS_INGESTA.
#   todos (por defecto desde el 20/11/2026): las vigentes de TODOS los rubros que trae la API, de bienes,
#     servicios y obras. La lectura de bases atiende primero a piloto y solicitados.
#   solicitados: solo los piloto y los solicitados por las empresas (EN-011). Como los piloto son de bienes,
#     servicios y obras solo entran si se solicita alguno de sus rubros.
RUBROS_MODE = os.environ.get("RUBROS_INGESTA", "").strip().lower() or "todos"
ALL_RUBROS = RUBROS_MODE not in ("solicitados", "piloto")
# Categorías que guarda la ingesta: bienes, servicios y obras (pedido del asesor, 19/11/2026). Con
# CATEGORIAS_INGESTA=bienes se vuelve al alcance anterior. Los rubros (piloto, solicitados o todos) se
# aplican dentro de cada categoría.
_CATEGORY_NAMES = ("bienes", "servicios", "obras")
CATEGORIES = tuple(c for c in (p.strip().lower() for p in os.environ.get("CATEGORIAS_INGESTA", ",".join(_CATEGORY_NAMES)).split(","))
                   if c in _CATEGORY_NAMES) or _CATEGORY_NAMES

# RAG de las bases (EN-012). Modelo de embeddings de Gemini y tamaño del vector: 768 es una de las
# dimensiones que recomienda Google para gemini-embedding-001 (por defecto entrega 3072).
EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "").strip() or "gemini-embedding-001"
EMBEDDING_DIM = 768
# Bases que se descargan e indexan por ejecución de la ingesta (cada una tarda segundos, o minutos con OCR)
RAG_MAX_BASES_PER_RUN = int(os.environ.get("RAG_MAX_BASES_PER_RUN", "").strip() or "25")
# Tamaño máximo de las bases que descarga la lectura (la pantalla de descarga mantiene 80 MB).
# El 03/11/2026, 2 de 142 bases vigentes pasaban de 80 MB.
RAG_MAX_BASES_MB = int(os.environ.get("RAG_MAX_BASES_MB", "").strip() or "250")
# HU-018: cuántos cronogramas lee cada ingesta (bases nuevas o cuya etapa cambió). Se leen sin OCR y
# sin Gemini; las que no alcanzan quedan para la siguiente ejecución.
SCHEDULE_MAX_PER_RUN = int(os.environ.get("CRONOGRAMA_MAX_POR_EJECUCION", "").strip() or "60")
# Bases que se leen a la vez (20/11/2026). Casi todo el tiempo es la descarga desde el SEACE, así que varias
# a la vez acortan la lectura. 1 = una por una, como antes. Más de 8 no se permite, para no saturar al SEACE.
RAG_WORKERS = min(8, max(1, int(os.environ.get("RAG_HILOS", "").strip() or "1")))
# Lectura de las bases escaneadas (20/11/2026). «gemini»: las páginas escaneadas se leen con Gemini, que
# entiende la imagen de la página (mejor que tesseract en sellos, tablas y fotos torcidas); si Gemini falla,
# se usa tesseract. «tesseract»: solo tesseract, como antes. «auto» (por defecto): gemini si hay clave.
# Solo se envían bases de licitación, que son públicas; nunca los documentos privados de las empresas.
OCR_ENGINE = (os.environ.get("OCR_MOTOR", "").strip().lower() or "auto")
GEMINI_OCR_MODEL = os.environ.get("GEMINI_OCR_MODEL", "").strip() or MODEL
# Análisis de los requisitos con Gemini (20/11/2026): cada requisito de las bases se convierte en datos (monto
# mínimo, profesión y meses, registro del RNP…) para compararlos con los de la empresa. ANALISIS_BASES=no lo apaga.
BASES_ANALYSIS = os.environ.get("ANALISIS_BASES", "").strip().lower() not in ("no", "0", "false")
# Tipo de cambio de referencia para comparar requisitos en dólares con montos en soles. 3,40: cierre del BCRP
# del 13/11/2026 (S/ 3,4050). Las bases suelen indicar cuál usar; la ficha lo dice al comparar.
USD_RATE = float(os.environ.get("TIPO_CAMBIO_USD", "").strip() or "3.40")
# Tope de fragmentos por minuto que se envían a Gemini para los vectores (0 = sin tope: se aprende con el
# primer 429). El límite es por proyecto de Google y se ve en https://aistudio.google.com/rate-limit.
GEMINI_VECTORS_PER_MINUTE = max(0, int(os.environ.get("GEMINI_VECTORES_POR_MINUTO", "").strip() or "0"))

# Recuperación por correo. Resend por HTTPS permite operar en Render Free,
# que bloquea los puertos SMTP habituales. No se habilita sin configuración.
PASSWORD_MANAGEMENT_ENABLED = os.environ.get("PASSWORD_MANAGEMENT_ENABLED", "").strip().lower() in {"1", "true"}
EMAIL_TRANSPORT = os.environ.get("EMAIL_TRANSPORT", "smtp").strip().lower()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = os.environ.get("SMTP_PORT", "587").strip()
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip()
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()