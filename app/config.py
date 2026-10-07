"""Configuración de la aplicación. Los valores sensibles vienen de variables de entorno."""
import os

from dotenv import load_dotenv

load_dotenv()

APP_NOMBRE = "LicitaFácil"

# Claves: en local, desde el archivo .env; en Railway, desde sus variables de entorno
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY", "solo-para-desarrollo-local")

# Modelo elegido en SP-004; gemini-3.5-flash-lite es la alternativa documentada
MODELO = os.environ.get("MODELO_GEMINI", "gemini-3.1-flash-lite")

# Reintentos ante saturación del servidor: cortos, porque el usuario está esperando
REINTENTOS = 2
ESPERA_REINTENTO_SEGUNDOS = 2

LARGO_MAXIMO_DESCRIPCION = 300