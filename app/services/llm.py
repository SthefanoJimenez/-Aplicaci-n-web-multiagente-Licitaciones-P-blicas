import json
import re
import time

from google import genai
from google.genai import errors, types
from langsmith import traceable

import config


class ModelError(Exception):
    """El modelo no respondió o respondió en un formato inesperado.

    code: código HTTP de la API (429 límite de uso, 503 servicio saturado...) o None.
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


RETRYABLE = (429, 500, 503)


_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise ModelError(
                "No se ha configurado la clave de API de Gemini (GEMINI_API_KEY). "
                "Configure GEMINI_API_KEY en su archivo .env o en las variables de entorno.",
                code=401,
            )
        try:
            _client = genai.Client(api_key=config.GEMINI_API_KEY)
        except Exception as e:
            raise ModelError(f"Error al inicializar el cliente de Gemini: {e}", code=401) from e
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


def _generate_with(prompt, model):
    """Llama a un modelo con reintentos ante errores temporales."""
    for attempt in range(1, config.RETRIES + 2):
        try:
            result = _call_gemini(prompt, model, langsmith_extra={"metadata": {"ls_model_name": model}})
            text = re.sub(r"^```(?:json)?|```$", "", result["text"].strip()).strip()
            return json.loads(text)
        except errors.APIError as e:
            if e.code in RETRYABLE and attempt <= config.RETRIES:
                time.sleep(config.RETRY_WAIT_SECONDS * attempt)
                continue
            raise ModelError(f"El modelo {model} respondió con el error {e.code}", code=e.code) from e
        except (json.JSONDecodeError, TypeError) as e:
            raise ModelError("El modelo devolvió una respuesta que no es JSON") from e
        except ValueError as e:
            raise ModelError(f"Error de configuración del modelo: {e}", code=401) from e
    raise ModelError("El modelo no respondió")


def generate_json(prompt, model=None):
    """Envía el prompt al modelo y devuelve su respuesta JSON como diccionario.

    Si el modelo principal sigue fallando por un error temporal y hay un modelo de
    respaldo configurado (GEMINI_FALLBACK_MODEL), lo intenta una vez con ese modelo.
    """
    model = model or config.MODEL
    try:
        return _generate_with(prompt, model)
    except ModelError as e:
        fallback = config.FALLBACK_MODEL
        if e.code in RETRYABLE and fallback and fallback != model:
            return _generate_with(prompt, fallback)
        raise

# ---------------------------------------------------------------------------
# Embeddings para el RAG de las bases (EN-012)
# ---------------------------------------------------------------------------
EMBED_BATCH = 50          # textos por llamada


@traceable(run_type="embedding", name="Gemini embeddings", metadata={"ls_provider": "google_genai"})
def _call_embed(texts, task_type, model):
    try:
        response = _get_client().models.embed_content(
            model=model,
            contents=texts,
            config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=config.EMBEDDING_DIM),
        )
        return [list(e.values) for e in response.embeddings]
    except errors.APIError:
        raise
    except Exception as e:
        raise ModelError(f"Error al generar embeddings: {e}", code=401) from e


def _normalize(vector):
    """Con menos de 3072 dimensiones, Google indica normalizar el vector antes de compararlo."""
    norm = sum(v * v for v in vector) ** 0.5
    return [v / norm for v in vector] if norm else vector


def embed_texts(texts, task_type="RETRIEVAL_DOCUMENT", model=None):
    """Vectores de los textos (uno por texto, en el mismo orden), con reintentos ante errores temporales.

    task_type: "RETRIEVAL_DOCUMENT" para los fragmentos que se guardan, "RETRIEVAL_QUERY" para la pregunta.
    """
    model = model or config.EMBEDDING_MODEL
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        for attempt in range(1, config.RETRIES + 2):
            try:
                result = _call_embed(batch, task_type, model)
                break
            except errors.APIError as e:
                if e.code in RETRYABLE and attempt <= config.RETRIES:
                    time.sleep(config.RETRY_WAIT_SECONDS * attempt)
                    continue
                raise ModelError(f"El modelo {model} respondió con el error {e.code}", code=e.code) from e
        if len(result) != len(batch):
            raise ModelError("El modelo devolvió un número de vectores distinto al de textos")
        vectors.extend(_normalize(v) for v in result)
    return vectors
