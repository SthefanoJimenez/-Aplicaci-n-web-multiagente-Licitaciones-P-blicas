"""Analítica descriptiva sobre los datos planos: panorama y detalle del mercado.

Cálculos deterministas con pandas sobre data.AWARDS_DETAIL: no usan el modelo de
lenguaje. Los usan el panorama del mercado (/panorama), el detalle del bien en la
pantalla de mercado y la página "Cómo preparamos los datos".
"""
from functools import lru_cache
import math

import pandas as pd

import rules
from services import data

LOCAL_DEPARTMENT = "LA LIBERTAD"
PERSONA_NATURAL = "Persona natural"

# Tamaño de la compra: monto adjudicado del ítem, en soles
SIZE_BUCKETS = [
    ("hasta-50k", "Hasta S/ 50 mil", 0, 50_000),
    ("50k-100k", "S/ 50 mil a 100 mil", 50_000, 100_000),
    ("100k-250k", "S/ 100 mil a 250 mil", 100_000, 250_000),
    ("250k-1m", "S/ 250 mil a 1 millón", 250_000, 1_000_000),
    ("mas-1m", "Más de S/ 1 millón", 1_000_000, math.inf),
]
SIZE_LABELS = {key: label for key, label, _, _ in SIZE_BUCKETS}
# Etiquetas cortas para los ejes de las columnas (el monto en soles ya se indica en el título).
SIZE_SHORT = {"hasta-50k": "Hasta 50 mil", "50k-100k": "50 a 100 mil", "100k-250k": "100 a 250 mil",
              "250k-1m": "250 mil a 1 M", "mas-1m": "Más de 1 M"}

# Tipos de proceso: las variantes legales se agrupan en su procedimiento principal
PROCESS_GROUPS = [
    ("Subasta Inversa Electrónica", "Subasta inversa electrónica"),
    ("Licitación Pública Abreviada", "Licitación pública abreviada"),
    ("Licitación Pública", "Licitación pública"),
    ("Adjudicación Simplificada", "Adjudicación simplificada"),
    ("Comparación de Precios", "Comparación de precios"),
]
OTHER_PROCESS = "Otros procedimientos"


def process_group(name):
    text = str(name)
    for prefix, label in PROCESS_GROUPS:
        if text.startswith(prefix):
            return label
    return OTHER_PROCESS


def size_bucket(amount):
    for key, _, low, high in SIZE_BUCKETS:
        if low < amount <= high or (low == 0 and amount <= high):
            return key
    return SIZE_BUCKETS[-1][0]


def _prepare(table):
    """Columnas derivadas que usan todos los cálculos. Se agregan sobre la misma tabla
    (sin copiarla) para no duplicar unos 50 MB de memoria en cada proceso del servidor."""
    t = table
    t["lugar"] = t["departamento_entrega"].fillna(t["entidad_departamento"])
    t["proceso_grupo"] = t["tipo_proceso"].map(process_group)
    t["tamanio"] = t["monto_adjudicado"].map(size_bucket)
    # Año y mes de la buena pro: los filtros de tiempo del panorama usan esta fecha
    t["bp_year"] = pd.to_numeric(t["fecha_buenapro"].str.slice(0, 4), errors="coerce").astype("Int64")
    t["bp_month"] = pd.to_numeric(t["fecha_buenapro"].str.slice(5, 7), errors="coerce").astype("Int64")
    return t


# Tipos de entidad tal como los publica el OECE, escritos para un empresario
ENTITY_TYPE_LABELS = {
    "GOBIERNO LOCAL": "Gobierno local (municipalidades)",
    "GOBIERNO REGIONAL": "Gobierno regional",
    "GOBIERNO NACIONAL": "Gobierno nacional (ministerios y organismos)",
    "FONAFE": "Empresas del Estado (FONAFE)",
    "PETROPERU": "Petroperú",
}


def entity_type_label(value):
    return ENTITY_TYPE_LABELS.get(value, str(value).capitalize() if isinstance(value, str) else "Sin dato")


DETAIL = _prepare(data.AWARDS_DETAIL)
YEARS = sorted(int(y) for y in DETAIL["anio"].unique())
DEPARTMENTS = sorted(d for d in DETAIL["lugar"].dropna().unique())
PROCESS_OPTIONS = [label for _, label in PROCESS_GROUPS] + [OTHER_PROCESS]
ENTITY_TYPES = [t for t in DETAIL["tipo_entidad"].value_counts().index]
BP_YEARS = sorted(int(y) for y in DETAIL["bp_year"].dropna().unique())
MONTH_NAMES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
               "setiembre", "octubre", "noviembre", "diciembre"]

# Compras de monto alto: la compra típica (mediana) de un bien está entre el 10% más alto del
# catálogo. Es un umbral calculado con los mismos datos, no una regla legal.
_GOOD_MEDIAN_AMOUNT = DETAIL[DETAIL["codigoitem"].isin(data.VALID_CODES)].groupby("codigoitem")["monto_adjudicado"].median()
HIGH_AMOUNT_THRESHOLD = float(_GOOD_MEDIAN_AMOUNT.quantile(0.9))
ENTITY_TYPE_OPTIONS = [(t, entity_type_label(t)) for t in ENTITY_TYPES]


def _text(value):
    """Texto para las plantillas: los vacíos de pandas (NA, NaN) pasan como None.

    Jinja evalúa `a or b`, y pd.NA no se puede evaluar como verdadero o falso.
    """
    return None if value is None or pd.isna(value) else str(value)


def _share_single(bidders):
    with_data = bidders.dropna()
    return 100 * float((with_data == 1).mean()) if len(with_data) else None


def _bars(series, top=None):
    """Lista [{label, value, pct}] con pct relativo al máximo (para dibujar barras)."""
    items = series.head(top) if top else series
    peak = float(items.max()) if len(items) else 0
    return [{"label": str(k), "value": float(v), "pct": 100 * float(v) / peak if peak else 0}
            for k, v in items.items()]


MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "set", "oct", "nov", "dic"]
MAP_STEPS = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]   # azul secuencial, claro = pocas compras


def _by_month(t, year=None, month=None):
    """Compras y monto por mes de buena pro, del primer al último mes con datos (incluye meses sin compras)."""
    year_month = t["fecha_buenapro"].str.slice(0, 7)
    g = t.assign(_m=year_month).groupby("_m").agg(n=("monto_adjudicado", "size"), amount=("monto_adjudicado", "sum"))
    if g.empty:
        return []
    first, last = g.index.min(), g.index.max()
    keys, (y, m) = [], (int(first[:4]), int(first[5:]))
    while f"{y}-{m:02d}" <= last:
        keys.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if month:   # con filtro de mes solo se muestran ese mes de cada año
        keys = [k for k in keys if int(k[5:]) == month]
    g = g.reindex(keys, fill_value=0)
    return [{"key": k, "label": f"{MONTHS_ES[int(k[5:]) - 1]} {k[:4]}", "n": int(r.n), "amount": float(r.amount)}
            for k, r in zip(keys, g.itertuples())]


PLACE_LABELS = {
    "MULTIDEPARTAMENTAL": "Multidepartamental (varios departamentos)",
    "EXTERIOR": "Exterior (fuera del Perú)",
}


def place_label(value):
    return PLACE_LABELS.get(value, str(value).title())


def _special_places(t, department):
    """Lugares de entrega que no son un departamento: el OECE no detalla dónde se entregan."""
    counts = t["lugar"].value_counts()
    out = {"multi": int(counts.get("MULTIDEPARTAMENTAL", 0)), "exterior": int(counts.get("EXTERIOR", 0))}
    if department == "MULTIDEPARTAMENTAL" and len(t):
        by_entity_place = t["entidad_departamento"].value_counts()
        top = t["entidad"].value_counts()
        out["entity_places"] = [{"label": k, "n": int(v), "pct": 100 * v / len(t)} for k, v in by_entity_place.items()]
        out["top_entity"] = {"name": top.index[0], "n": int(top.iloc[0]), "pct": 100 * top.iloc[0] / len(t)}
    if department == "EXTERIOR" and len(t):
        rows = t.sort_values("fecha_buenapro", ascending=False)
        out["exterior_rows"] = [{"date": _text(r.fecha_buenapro), "entity": _text(r.entidad), "good": _text(r.itemcubso),
                                 "supplier": _text(r.proveedor), "amount": float(r.monto_adjudicado),
                                 "nomenclature": _text(r.proceso)} for r in rows.itertuples()]
    return out


def _map_buckets(by_place):
    """Compras por departamento con su tono en el mapa (quintiles de los departamentos con compras)."""
    counts = by_place["n"]
    counts = counts[~counts.index.isin(NOT_A_DEPARTMENT)]
    if counts.empty:
        return {"departments": {}, "legend": []}
    cuts = sorted(set(counts.quantile([0.2, 0.4, 0.6, 0.8]).round().astype(int).tolist()))
    def step(n):
        return sum(n > c for c in cuts)
    edges = [int(counts.min())] + [c + 1 for c in cuts]
    uppers = cuts + [int(counts.max())]
    legend = [{"color": MAP_STEPS[min(i, 4)], "low": lo, "high": hi} for i, (lo, hi) in enumerate(zip(edges, uppers)) if lo <= hi]
    return {"departments": {name: {"n": int(n), "amount": float(by_place.loc[name, "amount"]),
                                   "color": MAP_STEPS[min(step(n), 4)]} for name, n in counts.items()},
            "legend": legend}


def _entity_types(t):
    """Compras, monto total y monto típico por tipo de entidad compradora."""
    g = (t.groupby("tipo_entidad")
         .agg(n=("monto_adjudicado", "size"), amount=("monto_adjudicado", "sum"),
              median_amount=("monto_adjudicado", "median"), median_price=("precio_unitario", "median"))
         .sort_values("n", ascending=False))
    peak, total = (int(g["n"].max()), int(g["n"].sum())) if len(g) else (0, 0)
    return [{"key": r.tipo_entidad, "label": entity_type_label(r.tipo_entidad), "n": int(r.n),
             "amount": float(r.amount), "median_amount": float(r.median_amount),
             "median_price": float(r.median_price),
             "pct": 100 * r.n / peak if peak else 0, "share": 100 * r.n / total if total else 0}
            for r in g.reset_index().itertuples()]


# ---------------------------------------------------------------------------
# Panorama del mercado
# ---------------------------------------------------------------------------
def clean_filters(year=None, department=None, process=None, size=None, entity_type=None, month=None):
    """Valida los filtros que llegan por la URL; los desconocidos se ignoran.
    El año y el mes son los de la buena pro."""
    year = int(year) if str(year or "").isdigit() and int(year) in BP_YEARS else None
    month = int(month) if str(month or "").isdigit() and 1 <= int(month) <= 12 else None
    department = department if department in DEPARTMENTS else None
    process = process if process in PROCESS_OPTIONS else None
    size = size if size in SIZE_LABELS else None
    entity_type = entity_type if entity_type in ENTITY_TYPES else None
    return year, department, process, size, entity_type, month


def filtered(year=None, department=None, process=None, size=None, entity_type=None, month=None):
    """Adjudicaciones que cumplen los filtros del panorama."""
    t = DETAIL
    if year:
        t = t[t["bp_year"] == year]
    if month:
        t = t[t["bp_month"] == month]
    if department:
        t = t[t["lugar"] == department]
    if process:
        t = t[t["proceso_grupo"] == process]
    if size:
        t = t[t["tamanio"] == size]
    if entity_type:
        t = t[t["tipo_entidad"] == entity_type]
    return t


@lru_cache(maxsize=256)
def overview(year=None, department=None, process=None, size=None, entity_type=None, month=None):
    t = filtered(year, department, process, size, entity_type, month)
    n = len(t)
    if n == 0:
        return {"n_awards": 0}

    by_place = t.groupby("lugar").agg(n=("monto_adjudicado", "size"), amount=("monto_adjudicado", "sum"))
    by_place = by_place.sort_values("n", ascending=False)
    by_size = t["tamanio"].value_counts().reindex([k for k, *_ in SIZE_BUCKETS], fill_value=0)
    by_process = t["proceso_grupo"].value_counts()
    by_type = _entity_types(t)
    by_month = _by_month(t, year, month)
    peru_map = _map_buckets(by_place)
    by_year = t.groupby("anio").agg(n=("monto_adjudicado", "size"), amount=("monto_adjudicado", "sum"))

    goods = (t.groupby(["codigoitem", "itemcubso", "unidad_medida"])
             .agg(n=("precio_unitario", "size"), median=("precio_unitario", "median"),
                  bidders=("n_postores", "median"))
             .reset_index().sort_values("n", ascending=False).head(12))
    entities = (t.groupby(["entidad", "entidad_departamento"])
                .agg(n=("monto_adjudicado", "size"), amount=("monto_adjudicado", "sum"))
                .reset_index().sort_values("n", ascending=False).head(10))

    top_place = by_place.index[0]
    local = int(by_place["n"].get(LOCAL_DEPARTMENT, 0))
    small = int(by_size.iloc[:2].sum())
    return {
        "n_awards": n,
        "amount": float(t["monto_adjudicado"].sum()),
        "median_amount": float(t["monto_adjudicado"].median()),
        "n_entities": int(t["entidad"].nunique()),
        "n_goods": int(t["codigoitem"].nunique()),
        "median_bidders": float(t["n_postores"].median()) if t["n_postores"].notna().any() else None,
        "pct_single": _share_single(t["n_postores"]),
        "by_place": [{"label": r.lugar, "value": int(r.n), "amount": float(r.amount),
                      "pct": 100 * r.n / by_place["n"].max(), "local": r.lugar == LOCAL_DEPARTMENT}
                     for r in by_place.reset_index().itertuples()],
        "by_size": [{"key": k, "label": SIZE_LABELS[k], "short": SIZE_SHORT[k], "value": int(v),
                     "pct": 100 * v / by_size.max() if by_size.max() else 0,
                     "share": 100 * v / n} for k, v in by_size.items()],
        "by_process": _bars(by_process),
        "by_entity_type": by_type,
        "by_month": by_month,
        "special": _special_places(t, department),
        "n_without_date": int(t["fecha_buenapro"].isna().sum()),
        "map": peru_map,
        "by_year": [{"label": int(r.anio), "value": int(r.n), "amount": float(r.amount)}
                    for r in by_year.reset_index().itertuples()],
        "goods": [{"code": int(r.codigoitem), "name": r.itemcubso, "unit": r.unidad_medida, "n": int(r.n),
                   "median": float(r.median), "bidders": None if pd.isna(r.bidders) else float(r.bidders),
                   "in_catalog": int(r.codigoitem) in data.VALID_CODES}
                  for r in goods.itertuples()],
        "entities": [{"name": r.entidad, "department": r.entidad_departamento, "n": int(r.n), "amount": float(r.amount)}
                     for r in entities.itertuples()],
        "insights": {
            "top_place": top_place, "top_place_n": int(by_place["n"].iloc[0]),
            "local": local, "local_share": 100 * local / n,
            "small": small, "small_share": 100 * small / n,
            "top_process": by_process.index[0], "top_process_share": 100 * by_process.iloc[0] / n,
            "top_type": by_type[0]["label"], "top_type_share": by_type[0]["share"],
        },
    }


# ---------------------------------------------------------------------------
# Detalle del bien (pantalla de mercado)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=512)
def good_detail(code, unit):
    t = DETAIL[(DETAIL["codigoitem"] == code) & (DETAIL["unidad_medida"] == unit)]
    n = len(t)
    if n == 0:
        return None

    places = (t.groupby("lugar")
              .agg(n=("precio_unitario", "size"), median=("precio_unitario", "median"),
                   bidders=("n_postores", "median"),
                   single=("n_postores", lambda s: _share_single(s)))
              .sort_values(["n", "median"], ascending=[False, True]))
    entities = (t.groupby(["entidad", "lugar"])
                .agg(n=("monto_adjudicado", "size"), amount=("monto_adjudicado", "sum"))
                .reset_index().sort_values(["n", "amount"], ascending=False).head(8))
    companies = t[t["proveedor"] != PERSONA_NATURAL]
    suppliers = (companies.groupby("proveedor")
                 .agg(n=("monto_adjudicado", "size"), amount=("monto_adjudicado", "sum"),
                      median=("precio_unitario", "median"))
                 .reset_index().sort_values(["n", "amount"], ascending=False).head(8))
    by_size = t["tamanio"].value_counts().reindex([k for k, *_ in SIZE_BUCKETS], fill_value=0)
    local = t[t["lugar"] == LOCAL_DEPARTMENT]
    provinces = local["provincia_entrega"].fillna("Sin provincia").value_counts().head(6)
    recent = t.sort_values("fecha_buenapro", ascending=False).head(12)

    peak_place = int(places["n"].max())
    return {
        "n_awards": n,
        "n_places": int(len(places)),
        "n_entities": int(t["entidad"].nunique()),
        "n_suppliers": int(companies["proveedor"].nunique()),
        "n_natural": int((t["proveedor"] == PERSONA_NATURAL).sum()),
        "n_atypical": int(t["atipico"].sum()),
        "median_amount": float(t["monto_adjudicado"].median()),
        "median_quantity": float(t["cantidad"].median()),
        "places": [{"label": r.lugar, "n": int(r.n), "pct": 100 * r.n / peak_place, "median": float(r.median),
                    "bidders": None if pd.isna(r.bidders) else float(r.bidders),
                    "single": None if r.single is None or pd.isna(r.single) else float(r.single),
                    "local": r.lugar == LOCAL_DEPARTMENT}
                   for r in places.reset_index().itertuples()],
        "entities": [{"name": r.entidad, "place": r.lugar, "n": int(r.n), "amount": float(r.amount)}
                     for r in entities.itertuples()],
        "entity_types": _entity_types(t),
        "large": {
            "is_large": float(t["monto_adjudicado"].median()) >= HIGH_AMOUNT_THRESHOLD,
            "threshold": HIGH_AMOUNT_THRESHOLD,
            "share_over_1m": 100 * float((t["monto_adjudicado"] > 1_000_000).mean()),
            "share_over_250k": 100 * float((t["monto_adjudicado"] > 250_000).mean()),
        },
        "suppliers": [{"name": r.proveedor, "n": int(r.n), "amount": float(r.amount), "median": float(r.median)}
                      for r in suppliers.itertuples()],
        "by_size": [{"key": k, "label": SIZE_LABELS[k], "short": SIZE_SHORT[k], "value": int(v),
                     "pct": 100 * v / by_size.max() if by_size.max() else 0, "share": 100 * v / n}
                    for k, v in by_size.items()],
        "local_n": int(len(local)),
        "local_provinces": _bars(provinces),
        "recent": [{"date": _text(r.fecha_buenapro), "entity": _text(r.entidad), "place": _text(r.lugar),
                    "province": _text(r.provincia_entrega), "quantity": float(r.cantidad),
                    "price": float(r.precio_unitario), "amount": float(r.monto_adjudicado),
                    "bidders": None if pd.isna(r.n_postores) else int(r.n_postores),
                    "supplier": _text(r.proveedor), "atypical": bool(r.atipico), "process": _text(r.proceso_grupo),
                    "nomenclature": _text(r.proceso), "call": int(r.codigoconvocatoria), "item": int(r.n_item)}
                   for r in recent.itertuples()],
    }


# ---------------------------------------------------------------------------
# Cómo preparamos los datos
# ---------------------------------------------------------------------------
FUNNEL_HELP = {
    "Tabla integrada": "Adjudicaciones 2023-2025 unidas con sus convocatorias y postores",
    "Solo bienes": "Se quitan servicios, obras y consultorías",
    "Solo moneda nacional": "Se quitan compras en dólares u otras monedas",
    "Solo con código CUBSO válido": "Sin código del catálogo no se puede agrupar el bien",
    "Sin contratación directa": "Sin competencia: el precio no refleja el mercado",
    "Monto y cantidad mayores que cero": "Sin ellos no se puede calcular el precio unitario",
    "Solo la unidad mayoritaria de cada código": "Un bien comprado por kilo y por saco no se compara",
}


def funnel():
    rows = data.CLEANING_FUNNEL
    first = float(rows["filas"].iloc[0])
    steps, previous = [], None
    for r in rows.itertuples():
        steps.append({"name": r.paso, "help": FUNNEL_HELP.get(r.paso, ""), "rows": int(r.filas),
                      "pct": 100 * r.filas / first,
                      "removed": None if previous is None else int(previous - r.filas)})
        previous = r.filas
    return steps


def quality():
    return [{"year": int(r.anio), "awards": int(r.adjudicaciones), "goods": float(r.pct_bienes),
             "no_cubso": float(r.pct_sin_cubso), "no_bidders": float(r.pct_sin_postores)}
            for r in data.DATA_QUALITY.itertuples()]


def atypical_summary():
    return {"n": int(DETAIL["atipico"].sum()), "share": 100 * float(DETAIL["atipico"].mean()),
            "n_rows": int(len(DETAIL)), "n_catalog": len(data.VALID_CODES),
            "n_catalog_awards": int(len(data.AWARDS))}


# ---------------------------------------------------------------------------
# Descarga de los datos (para que el usuario verifique cada compra)
# ---------------------------------------------------------------------------
DOWNLOAD_COLUMNS = {
    "anio": "año", "fecha_buenapro": "fecha_buena_pro", "proceso": "nomenclatura_proceso",
    "codigoconvocatoria": "codigo_convocatoria", "n_item": "item", "tipo_proceso": "tipo_proceso",
    "entidad": "entidad", "tipo_entidad": "tipo_entidad", "lugar": "departamento_entrega",
    "provincia_entrega": "provincia_entrega", "codigoitem": "codigo_cubso", "itemcubso": "bien",
    "unidad_medida": "unidad_medida", "cantidad": "cantidad", "monto_adjudicado": "monto_adjudicado_soles",
    "precio_unitario": "precio_unitario_soles", "n_postores": "postores", "proveedor": "proveedor",
    "atipico": "precio_atipico",
}


def download_rows(table):
    """Tabla lista para descargar en CSV: columnas en español, más recientes primero."""
    out = table.sort_values("fecha_buenapro", ascending=False)[list(DOWNLOAD_COLUMNS)]
    return out.rename(columns=DOWNLOAD_COLUMNS)


def share_below(code, unit, cost):
    """Porcentaje de compras del bien que el Estado pagó a un precio unitario menor que el costo.
    No se registra en LangSmith: el costo no sale de esta función."""
    prices = DETAIL.loc[(DETAIL["codigoitem"] == code) & (DETAIL["unidad_medida"] == unit), "precio_unitario"]
    return 100 * float((prices < cost).mean()) if len(prices) else None


def good_rows(code, unit):
    return DETAIL[(DETAIL["codigoitem"] == code) & (DETAIL["unidad_medida"] == unit)]


# ---------------------------------------------------------------------------
# Página de inicio
# ---------------------------------------------------------------------------
LANDING_EXAMPLE = 12771            # cemento portland tipo I x 42.50 kg: muchas compras y precios estables
NOT_A_DEPARTMENT = {"EXTERIOR", "MULTIDEPARTAMENTAL"}


@lru_cache(maxsize=1)
def landing():
    """Cifras de la portada y un ejemplo real, calculados con todos los datos (sin muestreo)."""
    o = overview()
    example = None
    good = data.GOODS.get(LANDING_EXAMPLE)
    if good:
        rows = data.AWARDS[(data.AWARDS["codigoitem"] == LANDING_EXAMPLE)
                           & (data.AWARDS["unidad_medida"] == good["unit"])]
        if len(rows) >= rules.MIN_VERDICT:
            p25, median, p75 = (float(v) for v in rows["precio_unitario"].quantile([0.25, 0.5, 0.75]))
            detail = good_detail(LANDING_EXAMPLE, good["unit"])
            example = {
                "code": LANDING_EXAMPLE, "name": good["name"], "unit": good["unit"],
                "n_awards": int(len(rows)), "p25": p25, "median": median, "p75": p75,
                "dispersion": rules.dispersion(p25, median, p75),
                "median_bidders": float(rows["n_postores"].median()),
                "pct_single_bidder": _share_single(rows["n_postores"]),
                "local_n": detail["local_n"] if detail else 0,
                "n_entities": detail["n_entities"] if detail else None,
                "cost": round((p25 + median) / 2, 2),     # costo ilustrativo dentro de la mitad central
                # Una adjudicación real del ejemplo, para que el usuario la busque en el SEACE
                "sample": next((r for r in (detail["recent"] if detail else [])
                                if r["supplier"] and r["supplier"] != PERSONA_NATURAL and r["nomenclature"]), None),
            }
    years = (min(YEARS), max(YEARS))
    return {
        "n_awards": o["n_awards"], "amount": o["amount"], "n_entities": o["n_entities"],
        "n_goods": o["n_goods"], "n_catalog": len(data.VALID_CODES),
        "n_departments": len([d for d in DEPARTMENTS if d not in NOT_A_DEPARTMENT]),
        "n_suppliers": int(DETAIL.loc[DETAIL["proveedor"] != PERSONA_NATURAL, "proveedor"].nunique()),
        "local": o["insights"]["local"], "local_share": o["insights"]["local_share"],
        "years": years, "example": example,
    }