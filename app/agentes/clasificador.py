import re
import unicodedata
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

import datos
import llm

CARPETA_PROMPTS = Path(__file__).parent.parent / "prompts"
PROMPT_REFORMULAR = (CARPETA_PROMPTS / "clasificador_reformular.txt").read_text(encoding="utf-8")
PROMPT_ELEGIR = (CARPETA_PROMPTS / "clasificador_elegir.txt").read_text(encoding="utf-8")

MAX_CANDIDATOS = 3
CANDIDATOS_POR_BUSQUEDA = 30   


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/. ]", " ", texto)).strip()


_vectorizador = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
_matriz = _vectorizador.fit_transform(
    (datos.CATALOGO["itemcubso"] + " " + datos.CATALOGO["unidad_medida"]).map(normalizar)
)
_codigos_catalogo = datos.CATALOGO["codigoitem"].astype(int).tolist()


def preseleccionar(*textos):
    """Une los bienes más parecidos a cada texto, sin repetir, en orden de parecido."""
    codigos = []
    for texto in textos:
        if not texto:
            continue
        similitud = cosine_similarity(_vectorizador.transform([normalizar(texto)]), _matriz).ravel()
        for i in np.argsort(-similitud, kind="stable")[:CANDIDATOS_POR_BUSQUEDA]:
            if _codigos_catalogo[i] not in codigos:
                codigos.append(_codigos_catalogo[i])
    return codigos


def validar(codigos_propuestos):
    """Conserva solo los códigos que existen en el catálogo, sin repetir, hasta 3."""
    validos, descartados = [], []
    for codigo in codigos_propuestos:
        try:
            codigo = int(codigo)
        except (TypeError, ValueError):
            descartados.append(codigo)
            continue
        if codigo not in datos.CODIGOS_VALIDOS:
            descartados.append(codigo)
        elif codigo not in validos:
            validos.append(codigo)
    return validos[:MAX_CANDIDATOS], descartados


def clasificar(descripcion, llamar_modelo=llm.generar_json):
    """Devuelve los candidatos para la descripción.

    Resultado: {"candidatos": [{"codigo", "nombre", "unidad"}, ...],
                "descripcion_reformulada": str o None,
                "descartados": códigos propuestos por el modelo que no existen}
    Lanza llm.ErrorModelo si el modelo no responde en el paso de elección.
    """
    try:
        respuesta = llamar_modelo(PROMPT_REFORMULAR.replace("{{DESCRIPCION}}", descripcion))
        reformulada = str(respuesta.get("descripcion", "")).strip() or None
    except (llm.ErrorModelo, AttributeError):
        reformulada = None

    preseleccionados = preseleccionar(descripcion, reformulada)
    lista = "\n".join(
        f"{c} | {datos.BIENES[c]['nombre']} | {datos.BIENES[c]['unidad']}" for c in preseleccionados
    )

    respuesta = llamar_modelo(PROMPT_ELEGIR.replace("{{CATALOGO}}", lista).replace("{{DESCRIPCION}}", descripcion))
    propuestos = respuesta.get("codigos", []) if isinstance(respuesta, dict) else []

    validos, descartados = validar(propuestos if isinstance(propuestos, list) else [])
    return {
        "candidatos": [datos.BIENES[c] for c in validos],
        "descripcion_reformulada": reformulada,
        "descartados": descartados,
    }