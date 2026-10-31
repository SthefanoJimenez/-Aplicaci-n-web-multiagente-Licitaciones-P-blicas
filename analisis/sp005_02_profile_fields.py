"""SP-005 · Perfil de los campos del archivo mensual OCDS.

Revisa todas las convocatorias de un archivo mensual y cuenta qué valores toman
los campos que decide el filtro de "bienes vigentes": categoría, estados,
periodos (fecha límite), departamento, valor y documentos. Sirve para confirmar
con datos reales cómo detectar las convocatorias vigentes.

Uso, desde la carpeta analisis/ (con el archivo ya descargado en la caché por
"sp005_01_tender_source.py run", o bajado con curl):
    python sp005_02_profile_fields.py
    python sp005_02_profile_fields.py ruta/al/archivo.zip

Salidas: salidas/sp005/perfil_campos.txt y salidas/sp005/muestra_bienes.json
"""
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import sp005_01_tender_source as sp

TOP = 25


def walk_keys(obj, prefix=""):
    """Rutas de todas las claves de un diccionario (sin entrar en listas largas)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            yield path
            yield from walk_keys(value, path)
    elif isinstance(obj, list) and obj:
        yield from walk_keys(obj[0], prefix + "[]")


def main():
    sp.safe_console()
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        now = datetime.now(sp.LIMA)
        path = sp.CACHE_DIR / f"ocds_{now.year}_{now.month:02d}.bin"
    if not path.exists():
        sys.exit(f"No encuentro {path}. Ejecute antes 'python sp005_01_tender_source.py run' o indique la ruta.")

    releases = list(sp.compiled_releases(sp.read_ocds_file(path)))
    goods = [r for r in releases if sp.normalize((r.get("tender") or {}).get("mainProcurementCategory")) in sp.GOODS_VALUES]
    out = []

    def section(title, counter, total=None):
        out.append(f"\n## {title}")
        for value, n in counter.most_common(TOP):
            share = f" ({100 * n / total:.1f}%)" if total else ""
            out.append(f"  {n:>7}{share}  {value}")

    out.append(f"# Perfil de {path.name}")
    out.append(f"Convocatorias (procesos únicos): {len(releases)} · de bienes: {len(goods)}")

    section("Categoría (tender.mainProcurementCategory)",
            Counter((r.get("tender") or {}).get("mainProcurementCategory") for r in releases), len(releases))

    # En adelante, solo bienes
    tenders = [(r, r.get("tender") or {}) for r in goods]
    section("Bienes · tender.status", Counter(t.get("status") for _, t in tenders), len(goods))
    section("Bienes · items[].statusDetails",
            Counter(i.get("statusDetails") for _, t in tenders for i in t.get("items", [])))
    section("Bienes · items[].status", Counter(i.get("status") for _, t in tenders for i in t.get("items", [])))
    section("Bienes · tag de la última entrega", Counter(" + ".join(r.get("tag", [])) for r, _ in tenders), len(goods))

    def process_state(t):
        states = {sp.normalize(i.get("statusDetails")) for i in t.get("items", [])}
        if states == {"convocado"}:
            return "todos los ítems CONVOCADO"
        return "algún ítem CONVOCADO" if "convocado" in states else "ningún ítem CONVOCADO"
    section("Bienes · estado del proceso según sus ítems", Counter(process_state(t) for _, t in tenders), len(goods))
    open_ll = sum(1 for r, t in tenders if process_state(t) != "ningún ítem CONVOCADO"
                  and sp._department(sp._buyer_party(r))[0] == sp.LA_LIBERTAD)
    out.append(f"\nBienes con algún ítem CONVOCADO en La Libertad: {open_ll}")
    section("Bienes · mes de publicación (tender.datePublished)",
            Counter(str(t.get("datePublished") or "")[:7] or "sin fecha" for _, t in tenders), len(goods))

    period = Counter()
    for _, t in tenders:
        tp = t.get("tenderPeriod") or {}
        start, end = sp.parse_date(tp.get("startDate")), sp.parse_date(tp.get("endDate"))
        if not end:
            period["sin endDate"] += 1
        elif start and end <= start:
            period["endDate = startDate (un día)"] += 1
        else:
            period["endDate posterior a startDate"] += 1
    section("Bienes · tenderPeriod", period, len(goods))

    # Cualquier campo con "date" o "period" en su ruta: candidatos a fecha límite
    date_paths = Counter(p for _, t in tenders for p in set(walk_keys(t))
                         if any(w in p.lower() for w in ("date", "period", "deadline", "fecha")))
    section("Bienes · campos de fecha presentes en tender (candidatos a fecha límite)", date_paths, len(goods))

    section("Bienes · claves del comprador en parties[]",
            Counter(k for r, _ in tenders for k in sp._buyer_party(r).keys()))
    section("Bienes · claves de address del comprador",
            Counter(k for r, _ in tenders for k in (sp._buyer_party(r).get("address") or {}).keys()))
    departments = Counter(sp._department(sp._buyer_party(r))[0] for r, _ in tenders)
    section("Bienes · departamento del comprador", departments, len(goods))

    values = Counter("0 o vacío" if not sp._amount(t.get("value")) else "mayor que 0" for _, t in tenders)
    section("Bienes · tender.value.amount", values, len(goods))

    docs = [d for _, t in tenders for d in (t.get("documents") or [])]
    section("Bienes · documents[].documentType", Counter(d.get("documentType") for d in docs))
    section("Bienes · documents[].format", Counter(d.get("format") for d in docs))
    section("Bienes · documents[].title", Counter(d.get("title") for d in docs))
    bidding = [d for d in docs if d.get("documentType") == "biddingDocuments"]
    section("Bienes · formato de las bases (solo biddingDocuments)", Counter(d.get("format") for d in bidding), len(bidding))
    section("Bienes · título de las bases (solo biddingDocuments)", Counter(d.get("title") for d in bidding), len(bidding))
    section("Bienes · claves de tender (busque extensiones con estado o fechas)",
            Counter(k for _, t in tenders for k in t.keys()), len(goods))
    with_bases = sum(1 for _, t in tenders if any(d.get("documentType") == "biddingDocuments" for d in t.get("documents") or []))
    out.append(f"\nBienes con documento biddingDocuments: {with_bases} de {len(goods)}"
               f" ({100 * with_bases / len(goods):.1f}%)" if goods else "")

    report = "\n".join(out) + "\n"
    sp.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (sp.OUTPUT_DIR / "perfil_campos.txt").write_text(report, encoding="utf-8")
    sample = [r for r in goods if any(sp.normalize(i.get("statusDetails")) == "convocado"
                                      for i in (r.get("tender") or {}).get("items", []))][:3] or goods[:3]
    (sp.OUTPUT_DIR / "muestra_bienes.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)
    print(f"Guardado en {sp.OUTPUT_DIR / 'perfil_campos.txt'} y {sp.OUTPUT_DIR / 'muestra_bienes.json'}")


if __name__ == "__main__":
    main()