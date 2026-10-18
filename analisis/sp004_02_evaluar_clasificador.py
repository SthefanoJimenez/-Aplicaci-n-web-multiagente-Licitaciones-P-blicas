"""SP-004: evaluar el agente clasificador.

Estrategias:
  preseleccion (por defecto): el modelo reescribe la descripción en términos del
      catálogo, se preseleccionan los bienes más parecidos y el modelo elige
      entre esos candidatos. Envía unos pocos miles de tokens por consulta.
  completo: el modelo elige directamente entre los 2,023 bienes del catálogo.
      Envía unos 60 mil tokens por consulta. Se conserva para comparar.

Mide si el código correcto aparece entre los 3 candidatos, a nivel ítem y a
nivel familia, y si quedó dentro de la preselección.

Retoma donde quedó: si la cuota se agota, guarda el avance y, al volver a
ejecutar el mismo comando, solo consulta los casos pendientes.

Requiere GEMINI_API_KEY en un archivo .env (ver .env.example).
Uso:  python sp004_02_evaluar_clasificador.py <modelo> [<modelo> ...] [--estrategia completo]
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from utils import BASE, SALIDAS, guardar

PAUSA_SEGUNDOS = 15
REINTENTOS = 8
MAX_429_SEGUIDOS = 3
CANDIDATOS_POR_BUSQUEDA = 30     # por la descripción original y por la reformulada
PROMPTS = {"A": BASE / "prompts" / "sp004_prompt_a.txt",
           "B": BASE / "prompts" / "sp004_prompt_b.txt"}
PROMPT_REFORMULAR = BASE / "prompts" / "sp004_prompt_reformular.txt"
RUTA_DETALLE = SALIDAS / "sp004" / "resultados_detalle.csv"

parser = argparse.ArgumentParser()
parser.add_argument("modelos", nargs="+")
parser.add_argument("--estrategia", choices=["preseleccion", "completo"], default="preseleccion")
args = parser.parse_args()


class CuotaAgotada(Exception):
    pass


load_dotenv()
cliente = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

catalogo = pd.read_csv(BASE / "datos" / "produccion" / "catalogo.csv")
casos = pd.read_csv(BASE / "datos" / "sp004_casos_prueba.csv")
codigos_validos = set(catalogo["codigoitem"])
lineas = {r.codigoitem: f"{r.codigoitem} | {r.itemcubso} | {r.unidad_medida}" for r in catalogo.itertuples()}
familias = {
    r.id: set(catalogo.loc[catalogo["itemcubso"].str.contains(r.patron_familia, regex=True), "codigoitem"])
    for r in casos.itertuples()
}


# --- Preselección: similitud de texto entre la descripción y los nombres del catálogo ---
def normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/. ]", " ", texto)).strip()


vectorizador = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
matriz = vectorizador.fit_transform((catalogo["itemcubso"] + " " + catalogo["unidad_medida"]).map(normalizar))


def preseleccionar(*textos):
    """Une los bienes más parecidos a cada texto, sin repetir, en orden de parecido."""
    codigos = []
    for texto in textos:
        similitud = cosine_similarity(vectorizador.transform([normalizar(texto)]), matriz).ravel()
        for i in np.argsort(-similitud, kind="stable")[:CANDIDATOS_POR_BUSQUEDA]:
            codigo = int(catalogo["codigoitem"].iat[i])
            if codigo not in codigos:
                codigos.append(codigo)
    return codigos


# --- Llamadas al modelo ---
def consultar(modelo, prompt):
    """Llama al modelo y devuelve (texto, segundos, tokens de entrada)."""
    seguidos_429 = 0
    for intento in range(1, REINTENTOS + 1):
        try:
            inicio = time.perf_counter()
            respuesta = cliente.models.generate_content(
                model=modelo,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            segundos = time.perf_counter() - inicio
            uso = getattr(respuesta, "usage_metadata", None)
            return respuesta.text, segundos, getattr(uso, "prompt_token_count", None) or 0
        except errors.APIError as e:
            if e.code == 429:
                seguidos_429 += 1
                if seguidos_429 >= MAX_429_SEGUIDOS:
                    raise CuotaAgotada()
                espera = 60
            elif e.code == 503:
                seguidos_429 = 0
                espera = min(30 * intento, 120)
            else:
                seguidos_429 = 0
                espera = 15 * intento
            print(f"    Error {e.code} (intento {intento}/{REINTENTOS}); esperando {espera} s")
            time.sleep(espera)
    raise RuntimeError("El servidor no respondió tras varios intentos")


def leer_json(texto):
    return json.loads(re.sub(r"^```(?:json)?|```$", "", texto.strip()).strip())


# --- Avance de ejecuciones anteriores ---
resultados, reformulaciones = {}, {}
if RUTA_DETALLE.exists():
    previo = pd.read_csv(RUTA_DETALLE).fillna({"error": "", "candidatos": ""})
    if "estrategia" not in previo.columns:          # resultados de versiones anteriores
        previo["estrategia"] = "completo"
    for fila in previo.to_dict("records"):
        if fila["error"] == "":
            resultados[(fila["modelo"], fila["estrategia"], fila["prompt"], fila["id"])] = fila
            if fila["estrategia"] == "preseleccion" and isinstance(fila.get("descripcion_reformulada"), str):
                reformulaciones[(fila["modelo"], fila["id"])] = fila["descripcion_reformulada"]
    print(f"Se retoma la evaluación: {len(resultados)} casos ya respondidos.")


def guardar_avance():
    RUTA_DETALLE.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(resultados.values()).to_csv(RUTA_DETALLE, index=False, encoding="utf-8-sig")


plantilla_reformular = PROMPT_REFORMULAR.read_text(encoding="utf-8")
texto_catalogo = "\n".join(lineas.values())

try:
    for modelo in args.modelos:
        for version, ruta in PROMPTS.items():
            plantilla = ruta.read_text(encoding="utf-8")
            print(f"\n### Modelo {modelo} · Estrategia {args.estrategia} · Prompt {version}")
            for caso in casos.itertuples():
                clave = (modelo, args.estrategia, version, caso.id)
                if clave in resultados:
                    continue
                fila = {"modelo": modelo, "estrategia": args.estrategia, "prompt": version, "id": caso.id,
                        "descripcion": caso.descripcion, "codigo_correcto": caso.codigo_correcto}
                try:
                    tokens, segundos, llamadas = 0, 0.0, 0
                    if args.estrategia == "completo":
                        lista, reformulada, en_preseleccion = texto_catalogo, "", None
                        n_preseleccionados = len(lineas)
                    else:
                        reformulada = reformulaciones.get((modelo, caso.id))
                        if reformulada is None:
                            texto, s, t = consultar(modelo, plantilla_reformular.replace("{{DESCRIPCION}}", caso.descripcion))
                            reformulada = str(leer_json(texto).get("descripcion", caso.descripcion))
                            reformulaciones[(modelo, caso.id)] = reformulada
                            tokens, segundos, llamadas = tokens + t, segundos + s, llamadas + 1
                            time.sleep(PAUSA_SEGUNDOS)
                        candidatos_pre = preseleccionar(caso.descripcion, reformulada)
                        lista = "\n".join(lineas[c] for c in candidatos_pre)
                        en_preseleccion = caso.codigo_correcto in candidatos_pre
                        n_preseleccionados = len(candidatos_pre)

                    prompt = plantilla.replace("{{CATALOGO}}", lista).replace("{{DESCRIPCION}}", caso.descripcion)
                    texto, s, t = consultar(modelo, prompt)
                    tokens, segundos, llamadas = tokens + t, segundos + s, llamadas + 1
                    candidatos = [int(c) for c in leer_json(texto).get("codigos", [])][:3]
                    fila.update({
                        "descripcion_reformulada": reformulada,
                        "n_preseleccionados": n_preseleccionados,
                        "en_preseleccion": en_preseleccion,
                        "candidatos": " ".join(map(str, candidatos)),
                        "acierto_item": caso.codigo_correcto in candidatos,
                        "acierto_familia": any(c in familias[caso.id] for c in candidatos),
                        "candidatos_validos": all(c in codigos_validos for c in candidatos),
                        "llamadas": llamadas,
                        "segundos": round(segundos, 2),
                        "tokens_entrada": tokens,
                        "error": "",
                    })
                    marca_pre = "" if en_preseleccion is None else f"preselección={'✔' if en_preseleccion else '✘'} "
                    print(f"  {caso.id:>2} {marca_pre}ítem={'✔' if fila['acierto_item'] else '✘'} "
                          f"familia={'✔' if fila['acierto_familia'] else '✘'}  ({tokens:,} tokens)  {caso.descripcion}")
                except (CuotaAgotada, KeyboardInterrupt):
                    raise
                except Exception as e:
                    fila.update({"error": str(e)[:120]})
                    print(f"  {caso.id:>2} SIN RESPUESTA (se reintentará en la próxima ejecución)  {caso.descripcion}")
                resultados[clave] = fila
                guardar_avance()
                time.sleep(PAUSA_SEGUNDOS)
except CuotaAgotada:
    guardar_avance()
    sys.exit("\nCUOTA AGOTADA. El avance está guardado. Vuelva a ejecutar el mismo comando "
             "más tarde: continuará con los casos pendientes.")
except KeyboardInterrupt:
    guardar_avance()
    sys.exit("\nInterrumpido. El avance está guardado; puede continuar con el mismo comando.")

# Resumen sobre todos los casos respondidos, de esta y de ejecuciones anteriores
detalle = pd.DataFrame(resultados.values())
respondidos = detalle[detalle["error"] == ""]
resumen = (
    respondidos.groupby(["modelo", "estrategia", "prompt"])
    .agg(
        casos_evaluados=("id", "size"),
        en_preseleccion_pct=("en_preseleccion", lambda s: round(100 * s.astype(float).mean(), 1) if s.notna().any() else None),
        acierto_item_pct=("acierto_item", lambda s: round(100 * s.mean(), 1)),
        acierto_familia_pct=("acierto_familia", lambda s: round(100 * s.mean(), 1)),
        candidatos_validos_pct=("candidatos_validos", lambda s: round(100 * s.mean(), 1)),
        segundos_mediana=("segundos", "median"),
        tokens_entrada_mediana=("tokens_entrada", "median"),
    )
    .reset_index()
)
print("\n### RESUMEN (porcentajes sobre los casos respondidos)")
print(resumen.to_string(index=False))
guardar(detalle, "sp004/resultados_detalle.csv")
guardar(resumen, "sp004/resumen.csv")

pendientes = len(args.modelos) * len(PROMPTS) * len(casos) - len(
    respondidos[respondidos["modelo"].isin(args.modelos) & (respondidos["estrategia"] == args.estrategia)])
if pendientes > 0:
    print(f"\n  Faltan {pendientes} casos de esta ejecución. Vuelva a ejecutar el mismo comando.")