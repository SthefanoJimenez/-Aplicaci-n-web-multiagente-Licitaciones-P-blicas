import os

from dotenv import load_dotenv

load_dotenv()

APP_NOMBRE = "LicitaFácil"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY", "solo-para-desarrollo-local")

MODELO = os.environ.get("MODELO_GEMINI", "gemini-3.1-flash-lite")
REINTENTOS = 2
ESPERA_REINTENTO_SEGUNDOS = 2

LARGO_MAXIMO_DESCRIPCION = 300