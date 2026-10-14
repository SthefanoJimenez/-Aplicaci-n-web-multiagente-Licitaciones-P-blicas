import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents import classifier  
from services import data, llm  

PAUSE_SECONDS = 8
TESTS_DIR = Path(__file__).parent
queries = pd.read_csv(TESTS_DIR / "queries_hu001.csv")

rows = []
for q in queries.itertuples():
    row = {"id": q.id, "type": q.type, "description": q.description, "expected": q.expected}
    try:
        r = classifier.classify(q.description)
        codes = [c["code"] for c in r["candidates"]]
        reference = None if pd.isna(q.reference_code) else int(q.reference_code)
        row.update({
            "result": "candidates" if codes else "no_evidence",
            "candidates": " ".join(map(str, codes)),
            "names": " | ".join(c["name"] for c in r["candidates"]),
            "rephrased_description": r["rephrased_description"],
            "discarded_codes": " ".join(map(str, r["discarded"])),
            "all_in_catalog": all(code in data.VALID_CODES for code in codes),
            "reference_code": reference,
            "reference_among_candidates": (reference in codes) if reference else None,
            "error": "",
        })
        mark = "✔" if row["result"] == q.expected else "≠"
        print(f"{q.id:>2} {mark} {row['result']:<13} {row['candidates']:<22} {q.description}")
    except llm.ModelError as e:
        row.update({"result": "error", "error": str(e)})
        print(f"{q.id:>2} ERROR del modelo: {e}")
    rows.append(row)
    time.sleep(PAUSE_SECONDS)

detail = pd.DataFrame(rows)
detail.to_csv(TESTS_DIR / "results_hu001.csv", index=False, encoding="utf-8-sig")

answered = detail[detail["error"] == ""]
with_candidates = answered[answered["result"] == "candidates"]
with_reference = answered[answered["reference_code"].notna()]
print("\n### RESUMEN")
print(f"Consultas respondidas: {len(answered)} de {len(detail)}")
print(f"Candidatos que existen en el catálogo: "
      f"{100 * with_candidates['all_in_catalog'].mean() if len(with_candidates) else 100:.0f}%  "
      f"(criterio de aceptación: 100%)")
print(f"Códigos inexistentes propuestos por el modelo y descartados por la validación: "
      f"{(answered['discarded_codes'].fillna('') != '').sum()} consultas")
print(f"Resultado igual al esperado: {(answered['result'] == answered['expected']).sum()} de {len(answered)}")
print(f"Bien de referencia entre los candidatos: "
      f"{int(with_reference['reference_among_candidates'].fillna(False).sum())} de {len(with_reference)}")
print(f"\nDetalle guardado en {TESTS_DIR / 'results_hu001.csv'}")