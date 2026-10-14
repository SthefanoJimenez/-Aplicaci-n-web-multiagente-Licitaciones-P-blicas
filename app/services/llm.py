import json
import re
import time

from google import genai
from google.genai import errors, types
from langsmith import traceable

import config


class ModelError(Exception):
    """El modelo no respondió o respondió en un formato inesperado."""


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


@traceable(run_type="llm", name="Gemini", metadata={"ls_provider": "google_genai"})
def _call_gemini(prompt, model):
    """Una sola llamada al modelo. Devuelve el texto y los tokens consumidos."""
    response = _get_client().models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", None) or 0
    output_tokens = (getattr(usage, "candidates_token_count", None) or 0) + (
        getattr(usage, "thoughts_token_count", None) or 0
    )
    return {
        "text": response.text or "",
        "usage_metadata": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def generate_json(prompt, model=None):
    """Envía el prompt al modelo y devuelve su respuesta JSON como diccionario."""
    model = model or config.MODEL
    for attempt in range(1, config.RETRIES + 2):
        try:
            result = _call_gemini(prompt, model, langsmith_extra={"metadata": {"ls_model_name": model}})
            text = re.sub(r"^```(?:json)?|```$", "", result["text"].strip()).strip()
            return json.loads(text)
        except errors.APIError as e:
            if e.code in (429, 500, 503) and attempt <= config.RETRIES:
                time.sleep(config.RETRY_WAIT_SECONDS * attempt)
                continue
            raise ModelError(f"El modelo respondió con el error {e.code}") from e
        except (json.JSONDecodeError, TypeError) as e:
            raise ModelError("El modelo devolvió una respuesta que no es JSON") from e
    raise ModelError("El modelo no respondió")