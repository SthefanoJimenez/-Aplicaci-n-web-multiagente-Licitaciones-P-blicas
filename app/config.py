import os

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "LicitaFácil"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY", "solo-para-desarrollo-local")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

RETRIES = 2
RETRY_WAIT_SECONDS = 2

MAX_DESCRIPTION_LENGTH = 300