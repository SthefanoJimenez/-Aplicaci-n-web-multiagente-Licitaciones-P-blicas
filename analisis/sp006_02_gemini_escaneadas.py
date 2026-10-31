"""SP-006 · Prueba de Gemini con bases escaneadas (actividad 2).

Responde: ¿cuántos tokens cuesta que Gemini lea una base escaneada, y extrae bien los
requisitos? Compara dos caminos para cada base escaneada:

  pdf   Gemini lee el PDF completo (unos 258 tokens por página, según su documentación).
  ocr   Gemini lee solo la sección de requisitos que sacó el OCR local
        (sp006_01_leer_bases.py --ocr), igual que con las bases que tienen texto.

Por defecto solo CUENTA tokens (count_tokens): no pide ninguna respuesta al modelo.
Con --extraer, además pide la extracción de requisitos y registra los tokens reales.
Las llamadas con --extraer se trazan en LangSmith si LANGSMITH_TRACING=true.

Lee GEMINI_API_KEY y GEMINI_MODEL del .env de app/ (nunca se escriben en ningún archivo).
Las bases son documentos públicos del SEACE; no se envía ningún dato de usuarios.

Uso, desde analisis/ (después de sp006_01_leer_bases.py):
    python sp006_02_gemini_escaneadas.py                 cuenta tokens de las 3 escaneadas sugeridas
    python sp006_02_gemini_escaneadas.py --extraer       además prueba la extracción
    python sp006_02_gemini_escaneadas.py --codigos 1255745 --extraer
    python sp006_02_gemini_escaneadas.py --codigos "1255745,1253677" --extraer --caminos ocr
                                                         solo la sección del OCR (no sube el PDF)

Salidas en salidas/sp006/: gemini_escaneadas.csv y gemini/<código>_<camino>.json
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd

import sp005_01_tender_source as sp
import sp006_01_leer_bases as lb

OUTPUT_DIR = lb.OUTPUT_DIR

PROMPT = """Eres un asistente que lee bases de contrataciones públicas del Perú (bienes).
Del documento, extrae SOLO los requisitos de calificación que debe cumplir el postor.
Responde únicamente con JSON, con esta forma:
{"requisitos": [{"tipo": "...", "texto": "copia breve y literal del requisito", "pagina": 0}],
 "valor_estimado": "monto en soles tal como aparece, o null",
 "fecha_presentacion_ofertas": "fecha tal como aparece, o null"}
Reglas: no inventes nada; si un dato no aparece, usa null; "pagina" es la página del documento
donde aparece el requisito, o null si no se puede saber."""


def load_key():
    from dotenv import load_dotenv
    for env in (lb.ROOT.parent / "app" / ".env", Path(".env")):
        if env.exists():
            load_dotenv(env)
            break
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Falta GEMINI_API_KEY en app/.env")
    return key, os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")


def traced(function):
    """Traza en LangSmith si está instalado y activado; si no, la función queda igual."""
    try:
        from langsmith import traceable
    except ImportError:
        return function
    return traceable(run_type="llm", name="SP-006 Gemini", metadata={"ls_provider": "google_genai"})(function)


def usage_of(response):
    usage = getattr(response, "usage_metadata", None)
    tokens_in = getattr(usage, "prompt_token_count", None) or 0
    tokens_out = (getattr(usage, "candidates_token_count", None) or 0) + (getattr(usage, "thoughts_token_count", None) or 0)
    return {"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out}


def upload_pdf(client, path):
    """Sube el PDF con la Files API (los escaneados pesan de 27 a 46 MB) y espera a que esté listo."""
    uploaded = client.files.upload(file=str(path), config={"mime_type": "application/pdf"})
    for _ in range(60):
        state = str(getattr(getattr(uploaded, "state", None), "name", getattr(uploaded, "state", "")))
        if "PROCESSING" not in state.upper():
            break
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    return uploaded


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def run_one(client, model, codigo, way, contents, extract, pages):
    """Cuenta los tokens de una entrada y, con --extraer, pide la extracción."""
    from google.genai import types
    row = {"codigo": codigo, "camino": way, "paginas": pages, "modelo": model}
    row["tokens_entrada_conteo"] = client.models.count_tokens(model=model, contents=contents).total_tokens
    if not extract:
        return row, None

    @traced
    def extract_requirements(codigo, camino, paginas, langsmith_extra=None):
        response = client.models.generate_content(
            model=model, contents=contents + [PROMPT],
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"))
        return {"text": response.text or "", "usage_metadata": usage_of(response)}

    begin = time.perf_counter()
    result = extract_requirements(codigo, way, pages, langsmith_extra={"metadata": {"ls_model_name": model}})
    row["segundos"] = round(time.perf_counter() - begin, 1)
    row["tokens_entrada_real"] = result["usage_metadata"]["input_tokens"]
    row["tokens_salida"] = result["usage_metadata"]["output_tokens"]
    parsed = parse_json(result["text"])
    row["json_valido"] = parsed is not None
    row["requisitos_extraidos"] = len((parsed or {}).get("requisitos") or [])
    row["valor_estimado"] = (parsed or {}).get("valor_estimado")
    row["fecha_presentacion_ofertas"] = (parsed or {}).get("fecha_presentacion_ofertas")
    return row, parsed if parsed is not None else {"respuesta_no_json": result["text"]}


def main():
    sp.safe_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codigos", help="Códigos separados por comas (por defecto, las 3 escaneadas sugeridas).")
    parser.add_argument("--extraer", action="store_true", help="Además de contar, pedir la extracción (gasta cuota).")
    parser.add_argument("--caminos", default="pdf,ocr",
                        help='Caminos a probar: "pdf", "ocr" o "pdf,ocr" (por defecto). Con "ocr" no se sube el PDF.')
    args = parser.parse_args()

    from google import genai
    from google.genai import errors
    key, model = load_key()
    client = genai.Client(api_key=key)
    docs, _ = lb.load_sp005()
    codes = args.codigos.split(",") if args.codigos else list(lb.SUGGESTED_SCANNED)
    (OUTPUT_DIR / "gemini").mkdir(parents=True, exist_ok=True)
    rows = []
    for _, d in lb.selected_rows(docs, codes).iterrows():
        codigo = d["codigo"]
        path = lb.base_path(d)
        wanted = {w.strip() for w in args.caminos.split(",")}
        section = OUTPUT_DIR / "secciones" / f"{codigo}.txt"
        if wanted == {"ocr"} and not section.exists():
            print(f"  {codigo}: falta {section} (cópiela allí o ejecute sp006_01_leer_bases.py --ocr)")
            continue
        if "pdf" in wanted and (d["formato_real"] != "pdf" or not path.exists()):
            print(f"  {codigo}: se omite (no es un PDF descargado en {path.parent})")
            continue
        pages = int(d["pages"]) if pd.notna(d.get("pages")) else None
        ways = []
        uploaded = None
        try:
            if "pdf" in wanted:
                uploaded = upload_pdf(client, path)
                ways.append(("pdf", [uploaded]))
            if "ocr" in wanted and section.exists():
                ways.append(("ocr", [section.read_text(encoding="utf-8")]))
            elif "ocr" in wanted:
                print(f"  {codigo}: sin sección OCR en {section}; solo se mide el PDF")
            for way, contents in ways:
                try:
                    row, parsed = run_one(client, model, codigo, way, contents, args.extraer, pages)
                except errors.APIError as e:
                    row, parsed = {"codigo": codigo, "camino": way, "error": f"API {e.code}: {e.message}"[:200]}, None
                rows.append(row)
                if parsed is not None:
                    (OUTPUT_DIR / "gemini" / f"{codigo}_{way}.json").write_text(
                        json.dumps(parsed, ensure_ascii=False, indent=1), encoding="utf-8")
                extra = (f" · real {row.get('tokens_entrada_real'):,} + {row.get('tokens_salida'):,} de salida · "
                         f"{row.get('requisitos_extraidos')} requisitos") if "tokens_entrada_real" in row else ""
                print(f"  {codigo} [{way}]: " + (row.get("error") or f"{row['tokens_entrada_conteo']:,} tokens de entrada{extra}"))
        except errors.APIError as e:
            rows.append({"codigo": codigo, "camino": "pdf", "error": f"API {e.code}: {e.message}"[:200]})
            print(f"  {codigo}: error {e.code}")
        finally:
            if uploaded is not None:
                try:
                    client.files.delete(name=uploaded.name)
                except Exception:
                    pass
    table = pd.DataFrame(rows)
    previous_csv = OUTPUT_DIR / "gemini_escaneadas.csv"
    if previous_csv.exists() and len(table):
        # Conserva las mediciones anteriores de otros códigos o caminos (por ejemplo, el PDF ya medido)
        previous = pd.read_csv(previous_csv, dtype={"codigo": str})
        done = set(zip(table["codigo"].astype(str), table["camino"]))
        keep = previous[[(c, w) not in done for c, w in zip(previous["codigo"].astype(str), previous["camino"])]]
        table = pd.concat([keep, table], ignore_index=True)
    table.to_csv(previous_csv, index=False, encoding="utf-8-sig")
    if len(table) and "tokens_entrada_conteo" in table:
        summary = table.groupby("camino")["tokens_entrada_conteo"].median()
        print("\nTokens de entrada (mediana por camino): " + " · ".join(f"{k}: {int(v):,}" for k, v in summary.items()))
    print(f"Detalle: {OUTPUT_DIR / 'gemini_escaneadas.csv'}")


if __name__ == "__main__":
    main()