import re
import unicodedata
from pathlib import Path

import numpy as np
from langsmith import traceable
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from services import data, llm

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_REPHRASE = (PROMPTS_DIR / "classifier_rephrase.txt").read_text(encoding="utf-8")
PROMPT_CHOOSE = (PROMPTS_DIR / "classifier_choose.txt").read_text(encoding="utf-8")

MAX_CANDIDATES = 3
CANDIDATES_PER_SEARCH = 30


def normalize(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/. ]", " ", text)).strip()


_vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
_matrix = _vectorizer.fit_transform(
    (data.CATALOG["itemcubso"] + " " + data.CATALOG["unidad_medida"]).map(normalize)
)
_catalog_codes = data.CATALOG["codigoitem"].astype(int).tolist()


@traceable(run_type="tool", name="Preselect candidates")
def preselect(*texts):
    """Une los bienes más parecidos a cada texto, sin repetir, en orden de parecido."""
    codes = []
    for text in texts:
        if not text:
            continue
        similarity = cosine_similarity(_vectorizer.transform([normalize(text)]), _matrix).ravel()
        for i in np.argsort(-similarity, kind="stable")[:CANDIDATES_PER_SEARCH]:
            if _catalog_codes[i] not in codes:
                codes.append(_catalog_codes[i])
    return codes


@traceable(run_type="tool", name="Validate against catalog")
def validate(proposed_codes):
    """Conserva solo los códigos que existen en el catálogo, sin repetir, hasta 3."""
    valid, discarded = [], []
    for code in proposed_codes:
        try:
            code = int(code)
        except (TypeError, ValueError):
            discarded.append(code)
            continue
        if code not in data.VALID_CODES:
            discarded.append(code)
        elif code not in valid:
            valid.append(code)
    return valid[:MAX_CANDIDATES], discarded


@traceable(run_type="chain", name="Classifier agent",
           process_inputs=lambda inputs: {"description": inputs.get("description")})
def classify(description, call_model=llm.generate_json):
    """Devuelve los candidatos para la descripción.

    Resultado: {"candidates": [{"code", "name", "unit"}, ...],
                "rephrased_description": str o None,
                "discarded": códigos propuestos por el modelo que no existen}
    Lanza llm.ModelError si el modelo no responde en el paso de elección.
    """
    try:
        response = call_model(PROMPT_REPHRASE.replace("{{DESCRIPCION}}", description))
        rephrased = str(response.get("descripcion", "")).strip() or None
    except (llm.ModelError, AttributeError):
        rephrased = None

    preselected = preselect(description, rephrased)
    listing = "\n".join(
        f"{c} | {data.GOODS[c]['name']} | {data.GOODS[c]['unit']}" for c in preselected
    )

    response = call_model(PROMPT_CHOOSE.replace("{{CATALOGO}}", listing).replace("{{DESCRIPCION}}", description))
    proposed = response.get("codigos", []) if isinstance(response, dict) else []

    valid, discarded = validate(proposed if isinstance(proposed, list) else [])
    return {
        "candidates": [data.GOODS[c] for c in valid],
        "rephrased_description": rephrased,
        "discarded": discarded,
    }