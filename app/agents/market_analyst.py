import pandas as pd
from langsmith import traceable

import rules
from services import analytics, data

LOCAL_DEPARTMENT = "LA LIBERTAD"


@traceable(run_type="tool", name="Filter awards",
           process_outputs=lambda output: {"n_awards": len(output["prices"])})
def filter_awards(code, unit):
    """Datos de las adjudicaciones del bien, en su unidad de medida.

    "bidders" tiene un valor por adjudicación; None si no hay dato.
    """
    table = data.AWARDS
    rows = table[(table["codigoitem"] == code) & (table["unidad_medida"] == unit)]
    return {
        "prices": rows["precio_unitario"].tolist(),
        "years": rows["anio"].tolist(),
        "bidders": [None if pd.isna(n) else int(n) for n in rows["n_postores"]],
        "departments": rows["entidad_departamento"].tolist(),
    }


@traceable(run_type="tool", name="Compute price range",
           process_inputs=lambda inputs: {"n_prices": len(inputs.get("prices", []))})
def compute_price_range(prices):
    """P25, mediana y P75 con interpolación lineal (método de SP-002)."""
    series = pd.Series(prices, dtype="float64")
    p25, median, p75 = series.quantile([0.25, 0.50, 0.75], interpolation="linear").tolist()
    return {"p25": p25, "median": median, "p75": p75}


@traceable(run_type="tool", name="Compute competition",
           process_inputs=lambda inputs: {"n_awards": len(inputs.get("bidders", []))})
def compute_competition(bidders, departments):
    """Mediana de postores, % de procesos con un solo postor y adjudicaciones locales."""
    with_data = pd.Series([n for n in bidders if n is not None], dtype="float64")
    n_with_data = len(with_data)
    n_single = int((with_data == 1).sum())
    return {
        "n_with_bidders": n_with_data,
        "median_bidders": float(with_data.median()) if n_with_data else None,
        "n_single_bidder": n_single,
        "pct_single_bidder": 100 * n_single / n_with_data if n_with_data else None,
        "n_la_libertad": sum(d == LOCAL_DEPARTMENT for d in departments),
    }


@traceable(run_type="chain", name="Market analyst agent")
def analyze(code):
    """Devuelve el rango de precios, la competencia y la evidencia en que se basan.

    Resultado: {"code", "name", "unit", "n_awards", "year_from", "year_to",
                "p25", "median", "p75", "dispersion",
                "n_with_bidders", "median_bidders", "n_single_bidder", "pct_single_bidder",
                "n_la_libertad", "evidence"}
    "evidence" es uno de los resultados de rules.evidence_result().
    Si el bien no tiene adjudicaciones, los precios y el periodo son None.
    """
    good = data.GOODS.get(code)
    if good is None:
        return _no_data(code, name=None, unit=None)

    filtered = filter_awards(code, good["unit"])
    n = len(filtered["prices"])
    if n == 0:
        return _no_data(code, good["name"], good["unit"])

    price_range = compute_price_range(filtered["prices"])
    competition = compute_competition(filtered["bidders"], filtered["departments"])
    return {
        "code": code,
        "name": good["name"],
        "unit": good["unit"],
        "n_awards": n,
        "year_from": int(min(filtered["years"])),
        "year_to": int(max(filtered["years"])),
        **price_range,
        "dispersion": rules.dispersion(price_range["p25"], price_range["median"], price_range["p75"])
        if price_range["median"] > 0 else None,
        **competition,
        "evidence": rules.evidence_result(n),
    }


def _no_data(code, name, unit):
    return {
        "code": code, "name": name, "unit": unit, "n_awards": 0,
        "year_from": None, "year_to": None, "p25": None, "median": None, "p75": None,
        "dispersion": None, "n_with_bidders": 0, "median_bidders": None, "n_single_bidder": 0,
        "pct_single_bidder": None, "n_la_libertad": 0, "evidence": rules.evidence_result(0),
    }

@traceable(run_type="tool", name="Describe market",
           process_outputs=lambda output: {"n_awards": output["n_awards"] if output else 0})
def describe(code, unit):
    """Detalle descriptivo del mercado del bien (HU-002 y HU-003 ampliadas): lugares, entidades, proveedores,
    tamaño de las compras y últimas adjudicaciones. Determinista, sin modelo de lenguaje."""
    return analytics.good_detail(code, unit)