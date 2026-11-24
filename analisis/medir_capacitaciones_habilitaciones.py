"""Mide en Neon cuántas bases leídas piden capacitación del personal y habilitaciones (24/11/2026).

Solo lee: no escribe nada ni llama a Gemini. Se corre desde app/ (usa el DATABASE_URL del .env):

    cd app
    python ../analisis/medir_capacitaciones_habilitaciones.py

Sirve para decidir si vale la pena comparar capacitaciones y habilitaciones con la empresa.
"""
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
# app/services no tiene __init__.py: si el Python que se usa tiene instalado otro paquete llamado «services»
# (pasa con el Python de Homebrew), ese gana. app/services/__init__.py lo evita; si falta, se avisa.
import importlib.util                                          # noqa: E402
_spec = importlib.util.find_spec("services")
if _spec is None or not any(Path(p).resolve() == (Path.cwd() / "services").resolve()
                            for p in (_spec.submodule_search_locations or [])):
    sys.exit("Python está usando otro paquete «services» instalado en el sistema, no la carpeta app/services.\n"
             "Copie app/services/__init__.py (entregado el 24/11) y córralo de nuevo desde app/.")
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from sqlalchemy import select                                  # noqa: E402
from sqlalchemy.orm import undefer                             # noqa: E402

from services import db                                        # noqa: E402

TRAINING = re.compile(r"capacitaci[oó]n", re.I)
HOURS = re.compile(r"\b(\d{1,4})\s*(?:\(\w+\)\s*)?horas?(?:\s+lectivas|\s+acad[eé]micas|\s+cronol[oó]gicas)?", re.I)
FACTOR = re.compile(r"factor(?:es)? de evaluaci[oó]n|puntaje|puntos", re.I)


def main():
    engine = db.make_engine()
    if engine is None:
        sys.exit("Falta DATABASE_URL en el .env")
    session = db.make_session_factory(engine)()
    tenders = {t.id: t for t in session.scalars(select(db.Tender).options(undefer(db.Tender.bases_facts)))}
    read = defaultdict(set)
    training_hours, training_any, training_factor = defaultdict(set), defaultdict(set), defaultdict(set)
    examples = []
    for tender_id, text in session.execute(select(db.RequirementChunk.tender_id, db.RequirementChunk.text)):
        tender = tenders.get(tender_id)
        if tender is None:
            continue
        category = tender.category or "bienes"
        read[category].add(tender_id)
        for match in TRAINING.finditer(text or ""):
            window = text[max(0, match.start() - 300): match.end() + 300]
            training_any[category].add(tender_id)
            if FACTOR.search(window):
                training_factor[category].add(tender_id)
            if HOURS.search(window):
                training_hours[category].add(tender_id)
                if len(examples) < 12 and tender.code not in {e[0] for e in examples}:
                    examples.append((tender.code, category, " ".join(window.split())[:260]))

    analyzed, with_license, license_names = Counter(), Counter(), Counter()
    for tender in tenders.values():
        facts = tender.bases_facts or {}
        if not facts.get("requisitos") and "requisitos" not in facts:
            continue
        category = tender.category or "bienes"
        analyzed[category] += 1
        licenses = [r for r in facts.get("requisitos", []) if r.get("tipo") == "habilitacion"]
        if licenses:
            with_license[category] += 1
        for item in licenses:
            name = (item.get("documento") or item.get("descripcion") or "").strip().lower()
            license_names[" ".join(name.split())[:70]] += 1

    print("Capacitación del personal (en el texto leído de las bases)")
    for category in ("bienes", "servicios", "obras"):
        total = len(read[category])
        print(f"  {category:9}: {total:4} bases leídas · mencionan capacitación {len(training_any[category]):4}"
              f" · con horas {len(training_hours[category]):4} · como factor de evaluación {len(training_factor[category]):4}")
    print("\n  Ejemplos con horas:")
    for code, category, text in examples:
        print(f"   - {code} ({category}): …{text}…")

    print("\nHabilitaciones (requisitos analizados con Gemini)")
    for category in ("bienes", "servicios", "obras"):
        print(f"  {category:9}: {analyzed[category]:4} bases analizadas · piden al menos una habilitación "
              f"{with_license[category]:4}")
    print("\n  Las 25 habilitaciones más pedidas:")
    for name, count in license_names.most_common(25):
        print(f"   {count:4}  {name}")


if __name__ == "__main__":
    main()