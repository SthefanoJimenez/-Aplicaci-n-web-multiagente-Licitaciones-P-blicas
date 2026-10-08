import json
import re
import time

from google import genai
from google.genai import errors, types
try:
    from langsmith import traceable
except ImportError:
    def traceable(*args, **kwargs):
        def decorador(func):
            return func
        return decorador

import config


class ErrorModelo(Exception):
    """El modelo no respondió o respondió en un formato inesperado."""


_cliente = None


def _obtener_cliente():
    global _cliente
    if _cliente is None:
        _cliente = genai.Client(api_key=config.GEMINI_API_KEY)
    return _cliente


@traceable(run_type="llm", name="Gemini", metadata={"ls_provider": "google_genai"})
def _llamar_gemini(prompt, modelo, langsmith_extra=None):
    """Una sola llamada al modelo. Devuelve el texto y los tokens consumidos."""
    respuesta = _obtener_cliente().models.generate_content(
        model=modelo,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    uso = getattr(respuesta, "usage_metadata", None)
    entrada = getattr(uso, "prompt_token_count", None) or 0
    salida = (getattr(uso, "candidates_token_count", None) or 0) + (getattr(uso, "thoughts_token_count", None) or 0)
    return {
        "texto": respuesta.text or "",
        "usage_metadata": {"input_tokens": entrada, "output_tokens": salida, "total_tokens": entrada + salida},
    }


def generar_json(prompt, modelo=None):
    """Envía el prompt al modelo y devuelve su respuesta JSON como diccionario."""
    modelo = modelo or config.MODELO
    for intento in range(1, config.REINTENTOS + 2):
        try:
            resultado = _llamar_gemini(prompt, modelo, langsmith_extra={"metadata": {"ls_model_name": modelo}})
            texto = re.sub(r"^```(?:json)?|```$", "", resultado["texto"].strip()).strip()
            return json.loads(texto)
        except errors.APIError as e:
            # 429: límite de uso; 500 y 503: servidor saturado. Se reintenta brevemente.
            if e.code in (429, 500, 503) and intento <= config.REINTENTOS:
                time.sleep(config.ESPERA_REINTENTO_SEGUNDOS * intento)
                continue
            raise ErrorModelo(f"El modelo respondió con el error {e.code}") from e
        except (json.JSONDecodeError, TypeError) as e:
            raise ErrorModelo("El modelo devolvió una respuesta que no es JSON") from e
    raise ErrorModelo("El modelo no respondió")