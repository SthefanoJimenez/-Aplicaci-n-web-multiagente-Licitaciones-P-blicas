"""EN-012: mide el lector de bases del RAG con las bases reales del SP-005.

No usa la base de datos ni Gemini: solo abre cada base guardada en analisis/salidas/sp005/bases/,
busca la sección de requisitos (con OCR si tesseract está instalado) y la parte en fragmentos.

Uso, desde la raíz del repositorio:
    python analisis/en012_medir_bases.py

Escribe analisis/salidas/en012/medicion_bases.csv y muestra una línea por base y un resumen.
Con OCR tarda unos 20 minutos (las bases escaneadas son las lentas).
"""
import csv
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from services import bases_reader, rag  # noqa: E402

BASES = ROOT / "analisis" / "salidas" / "sp005" / "bases"
OUT = ROOT / "analisis" / "salidas" / "en012" / "medicion_bases.csv"


def main():
    files = sorted(p for p in BASES.iterdir() if p.is_file())
    # El SP-005 guardó algunos Word dos veces: como .zip (el original) y como .docx extraído
    files = [p for p in files if not (p.suffix == ".docx" and p.with_suffix(".zip").exists())]
    use_ocr = bases_reader.ocr_available()
    print(f"Medición del lector con {len(files)} bases del SP-005 · OCR {'disponible' if use_ocr else 'NO disponible'}\n",
          flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for number, path in enumerate(files, 1):
        started = time.perf_counter()
        result = bases_reader.read_bases(path.read_bytes(), use_ocr=use_ocr)
        section = result.section
        pieces = rag.chunk_section(section) if section else []
        seconds = round(time.perf_counter() - started)
        row = {"archivo": path.name, "estado": result.status, "fuente": section.source if section else "",
               "pagina_inicio": section.page_at(0) if section else "", "caracteres": len(section.text) if section else 0,
               "fragmentos": len(pieces), "tokens_est": sum(round(len(p) / bases_reader.CHARS_PER_TOKEN) for p, _ in pieces),
               "segundos": seconds, "detalle": result.detail}
        rows.append(row)
        page = f"pág. {row['pagina_inicio']}" if row["pagina_inicio"] else "-"
        code = re.search(r"seacev3-([0-9-]+)", path.stem)
        print(f"  {number:2d}/{len(files)} · {code.group(1) if code else path.stem} · {row['estado']:22s} · {row['fuente'] or '-':5s} · "
              f"{page:8s} · {row['fragmentos']:2d} fragmentos · {seconds:3d} s", flush=True)
    with OUT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    found = [r for r in rows if r["estado"] == "extraido"]
    counts = Counter(r["estado"] for r in rows)
    print(f"\nCon la sección: {len(found)} de {len(rows)} · " + " · ".join(f"{k}: {v}" for k, v in counts.items()))
    if found:
        print(f"Mediana por base: {statistics.median(r['fragmentos'] for r in found):.0f} fragmentos · "
              f"{statistics.median(r['tokens_est'] for r in found):,.0f} tokens estimados")
        print(f"Con OCR: {sum(r['fuente'] == 'ocr' for r in found)} · tiempo total: {sum(r['segundos'] for r in rows) / 60:.0f} min")
    print(f"Resultado guardado en {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()