import json
import re
import threading
import time

from google import genai
from google.genai import errors, types
from langsmith import traceable

import config


class ModelError(Exception):
    """El modelo no respondió o respondió en un formato inesperado.

    code: código HTTP de la API (429 límite de uso, 503 servicio saturado...) o None.
    """

    def __init__(self, message, code=None, detail=""):
        super().__init__(message)
        self.code = code
        self.detail = str(detail or "")      # texto completo de la API (dice si el límite es diario, de gasto…)


# ---------------------------------------------------------------------------
# Cuota agotada (20/11/2026): «usar Gemini y, ni bien se acaba, cambiar a tesseract»
# ---------------------------------------------------------------------------
# Cuando Gemini se queda sin cuota del día o sin saldo (tope de gasto, prepago en 0), no vale la pena seguir
# pidiéndole durante esta ejecución: la lectura de escaneadas pasa a tesseract, el análisis de requisitos se
# deja para después y las bases se guardan sin vectores (se completan con --completar-vectores).
_exhausted = {"texto": False, "vectores": False}
_exhausted_lock = threading.Lock()
OUT_OF_QUOTA_WORDS = ("perday", "per day", "per_day", "daily", "spend", "billing", "prepay", "credit", "cap ")


def out_of_quota(error):
    """True si el error dice que se acabó la cuota del día o el saldo (no un límite por minuto)."""
    if getattr(error, "code", None) == 402:
        return True
    if getattr(error, "code", None) != 429:
        return False
    detail = (getattr(error, "detail", "") or str(error)).lower()
    return any(word in detail for word in OUT_OF_QUOTA_WORDS)


def exhausted(kind):
    return _exhausted[kind]


def mark_exhausted(kind):
    """Marca la cuota de `kind` («texto» o «vectores») como agotada. True si recién se marcó."""
    with _exhausted_lock:
        new, _exhausted[kind] = not _exhausted[kind], True
        return new


def reset_exhausted():
    with _exhausted_lock:
        _exhausted.update(texto=False, vectores=False)


RETRYABLE = (429, 500, 503)


_client = None
_client_lock = threading.Lock()     # la lectura de bases puede pedir vectores desde varios hilos a la vez


def _get_client():
    with _client_lock:
        return _create_client() if _client is None else _client


def _create_client():
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


# Tareas de volumen (leer bases escaneadas, analizar requisitos): razonamiento mínimo y un tope de salida.
# El razonamiento («thinking») se cobra como salida (US$ 1,50 por millón en flash-lite) y no mejora una
# transcripción ni una extracción literal. 20/11/2026.
LOW_COST = {"thinking_level": "minimal"}


def _thinking(cheap):
    return types.ThinkingConfig(thinking_level=cheap["thinking_level"]) if cheap else None


@traceable(run_type="llm", name="Gemini", metadata={"ls_provider": "google_genai"})
def _call_gemini(prompt, model, cheap=None, max_tokens=None):
    """Una sola llamada al modelo. Devuelve el texto y los tokens consumidos."""
    response = _get_client().models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=_thinking(cheap),
            max_output_tokens=max_tokens,
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


def _generate_with(prompt, model, cheap=None, max_tokens=None):
    """Llama a un modelo con reintentos ante errores temporales."""
    for attempt in range(1, config.RETRIES + 2):
        try:
            extra = {k: v for k, v in (("cheap", cheap), ("max_tokens", max_tokens)) if v}
            result = _call_gemini(prompt, model, langsmith_extra={"metadata": {"ls_model_name": model}}, **extra)
            text = re.sub(r"^```(?:json)?|```$", "", result["text"].strip()).strip()
            return json.loads(text)
        except errors.APIError as e:
            if e.code == 400 and cheap:
                cheap = None            # un modelo que no admite razonamiento mínimo: se repite sin fijarlo
                continue
            if e.code in RETRYABLE and attempt <= config.RETRIES:
                time.sleep(config.RETRY_WAIT_SECONDS * attempt)
                continue
            raise ModelError(f"El modelo {model} respondió con el error {e.code}", code=e.code, detail=str(e)) from e
        except (json.JSONDecodeError, TypeError) as e:
            raise ModelError("El modelo devolvió una respuesta que no es JSON") from e
        except ValueError as e:
            raise ModelError(f"Error de configuración del modelo: {e}", code=401) from e
    raise ModelError("El modelo no respondió")


def generate_json(prompt, model=None, cheap=None, max_tokens=None):
    """Envía el prompt al modelo y devuelve su respuesta JSON como diccionario.

    Si el modelo principal sigue fallando por un error temporal y hay un modelo de
    respaldo configurado (GEMINI_FALLBACK_MODEL), lo intenta una vez con ese modelo.
    cheap=LOW_COST y max_tokens: para tareas de volumen (análisis de requisitos).
    """
    model = model or config.MODEL
    try:
        return _generate_with(prompt, model, cheap, max_tokens)
    except ModelError as e:
        fallback = config.FALLBACK_MODEL
        if e.code in RETRYABLE and fallback and fallback != model:
            return _generate_with(prompt, fallback, cheap, max_tokens)
        raise

OCR_MAX_TOKENS = 32000     # 10 páginas transcritas son unos 10 000 tokens; el resto es margen


def transcribe_pdf(pdf_bytes, prompt, model=None):
    """Texto que el modelo lee de un PDF (páginas escaneadas de unas bases). Devuelve (texto, tokens).

    Sin trazas de LangSmith: el PDF no se registra en ningún lado. Reintenta ante errores temporales y,
    si siguen, lanza ModelError (quien llama usa tesseract).
    """
    model = model or config.GEMINI_OCR_MODEL
    part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    cheap = LOW_COST
    for attempt in range(1, config.RETRIES + 2):
        try:
            response = _get_client().models.generate_content(
                model=model, contents=[part, prompt],
                config=types.GenerateContentConfig(
                    temperature=0, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    thinking_config=_thinking(cheap), max_output_tokens=OCR_MAX_TOKENS))
            usage = getattr(response, "usage_metadata", None)
            tokens = ((getattr(usage, "prompt_token_count", None) or 0)
                      + (getattr(usage, "candidates_token_count", None) or 0)
                      + (getattr(usage, "thoughts_token_count", None) or 0))
            return response.text or "", tokens
        except errors.APIError as e:
            if e.code == 400 and cheap:
                cheap = None
                continue
            if e.code in RETRYABLE and attempt <= config.RETRIES:
                time.sleep(config.RETRY_WAIT_SECONDS * attempt)
                continue
            raise ModelError(f"El modelo {model} respondió con el error {e.code}", code=e.code, detail=str(e)) from e
        except ValueError as e:
            raise ModelError(f"Error de configuración del modelo: {e}", code=401) from e
    raise ModelError("El modelo no respondió")


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
                raise ModelError(f"El modelo {model} respondió con el error {e.code}", code=e.code, detail=str(e)) from e
        if len(result) != len(batch):
            raise ModelError("El modelo devolvió un número de vectores distinto al de textos")
        vectors.extend(_normalize(v) for v in result)
    return vectors