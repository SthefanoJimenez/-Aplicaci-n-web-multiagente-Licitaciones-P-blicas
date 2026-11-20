"""¿A cuáles convocatorias vigentes puede postular mi empresa? (pedido del asesor, 19/11/2026)

El asesor pidió el cruce directo: con el perfil, los documentos y el personal que ya guarda la empresa,
decir con cuáles de todas las vigentes es candidata. Este módulo:
1. acota las vigentes a lo que la empresa ofrece: sus categorías (bienes, servicios, obras) y sus rubros
   (familias UNSPSC que se reconocen en el texto libre del perfil con services.rubros.match);
2. evalúa cada una con el mismo semáforo de la ficha (services.eligibility.evaluate_many);
3. las ordena: primero verde, luego amarillo y al final rojo; dentro de cada color, la que vence antes.

Para no leer cientos de bases en una sola visita, se evalúan como máximo MAX_EVALUATED convocatorias,
las más recientes del alcance; la pantalla dice cuántas quedaron fuera.
"""
import re

from services import accounts, eligibility, rubros, tenders

MAX_EVALUATED = 60
COLOR_ORDER = {"verde": 0, "amarillo": 1, "rojo": 2}


def company_scope(profile, names_map=None):
    """{"categories": [...], "rubros": [{"code","name"}], "declared": bool} a partir del perfil guardado."""
    if names_map is None:
        try:
            names_map = tenders.rubro_names()
        except tenders.TendersUnavailable:
            names_map = {}
    found, seen = [], set()
    for text in (profile.get("rubros"), profile.get("business_activity")):
        for code in re.findall(r"(?<!\d)(\d{4})(?!\d)", str(text or "")):
            if code not in seen:
                seen.add(code)
                found.append({"code": code, "name": rubros.clean_name(code, names_map.get(code) or f"Rubro {code}")})
        for item in rubros.match(text or "", names_map):
            if item["code"] not in seen:
                seen.add(item["code"])
                found.append({"code": item["code"], "name": item["name"]})
    categories = list(profile.get("categories") or [])
    return {"categories": categories, "rubros": found, "declared": bool(categories or found)}


def in_scope(tender, scope):
    """True si la convocatoria es de una categoría y un rubro que la empresa ofrece (lo no declarado no filtra)."""
    if scope["categories"] and tender.category not in scope["categories"]:
        return False
    codes = {item["code"] for item in scope["rubros"]}
    return not codes or bool(codes & set(tender.rubros or []))


def candidates(account_factory, user_id, open_rows, today=None, limit=MAX_EVALUATED):
    """Las vigentes del alcance de la empresa, evaluadas y ordenadas por semáforo.

    open_rows: convocatorias vigentes (contratos de services.tenders.open_tenders), ya filtradas por la
    pantalla si corresponde. Devuelve {"scope", "rows": [(tender, result)], "counts", "skipped"}.
    """
    profile = accounts.get_profile(account_factory, user_id)
    scope = company_scope(profile)
    rows = [t for t in open_rows if t.code and in_scope(t, scope)]
    evaluated, skipped = rows[:limit], max(0, len(rows) - limit)
    results = eligibility.evaluate_many(account_factory, user_id, [t.code for t in evaluated], today=today)
    pairs = [(t, results[str(t.code)]) for t in evaluated if str(t.code) in results]
    pairs.sort(key=lambda pair: (COLOR_ORDER.get(pair[1]["color"], 3), -_negative(pair[0])))
    counts = {color: sum(1 for _, r in pairs if r["color"] == color) for color in COLOR_ORDER}
    return {"scope": scope, "rows": pairs, "counts": counts, "skipped": skipped, "total": len(rows)}


def _negative(tender):
    """Para ordenar: primero la que vence (o se publicó) antes."""
    moment = tender.deadline_at or tender.published_at
    return -moment.timestamp() if moment else float("-inf")