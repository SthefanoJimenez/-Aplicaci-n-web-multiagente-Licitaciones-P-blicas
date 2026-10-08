import json
import re
import time

from google import genai
from google.genai import errors, types

import config


class ErrorModelo(Exception):
    """El modelo no respondió o respondió en un formato inesperado."""


_cliente = None


def _obtener_cliente():
    global _cliente
    if _cliente is None:
        _cliente = genai.Client(api_key=config.GEMINI_API_KEY)
    return _cliente


def generar_json(prompt, modelo=None):
    for intento in range(1, config.REINTENTOS + 2):
        try:
            respuesta = _obtener_cliente().models.generate_content(
                model=modelo or config.MODELO,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            texto = re.sub(r"^```(?:json)?|```$", "", (respuesta.text or "").strip()).strip()
            return json.loads(texto)
        except errors.APIError as e:
            if e.code in (429, 500, 503) and intento <= config.REINTENTOS:
                time.sleep(config.ESPERA_REINTENTO_SEGUNDOS * intento)
                continue
            raise ErrorModelo(f"El modelo respondió con el error {e.code}") from e
        except (json.JSONDecodeError, TypeError) as e:
            raise ErrorModelo("El modelo devolvió una respuesta que no es JSON") from e
    raise ErrorModelo("El modelo no respondió")