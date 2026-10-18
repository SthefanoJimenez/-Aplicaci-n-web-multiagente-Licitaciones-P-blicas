"""Agente redactor (HU-006, OE5): explica el veredicto en lenguaje simple.

Versión determinista con plantillas: el texto incluye el veredicto, el nivel de
confianza, el número de casos y las advertencias. No envía el costo a ningún
modelo de lenguaje.
"""
import rules
from services.formatting import format_pen as _pen, unit_text


def explain(evaluation, market, detail=None):
    e, m = evaluation, market
    unit = unit_text(m["unit"])
    diff = e["vs_median_pct"]
    if e["verdict"] == rules.PARTICIPATE:
        title = "Su costo es competitivo"
        text = (f"Su costo de {_pen(e['cost'])} por {unit} está por debajo del P25 ({_pen(m['p25'])}): es menor "
                f"que el precio que pagó el Estado en tres de cada cuatro compras. Tiene margen para ofertar un precio que suele ganar.")
    elif e["verdict"] == rules.PARTICIPATE_WITH_CAUTION:
        title = "Puede participar, con cuidado"
        text = (f"Su costo de {_pen(e['cost'])} por {unit} está dentro de la mitad central de los precios pagados "
                f"({_pen(m['p25'])} a {_pen(m['p75'])}). Hay empresas que venden más barato: revise su margen antes de ofertar.")
    else:
        title = "Con este costo, no le conviene"
        text = (f"Su costo de {_pen(e['cost'])} por {unit} es mayor que lo que pagó el Estado en tres de cada cuatro "
                f"compras ({_pen(m['p75'])} o menos). Para competir tendría que vender por debajo de su costo.")

    reasons = []
    if diff is not None:
        side = "por debajo" if diff < 0 else "por encima"
        reasons.append(f"Su costo está {abs(diff):.0f}% {side} del precio mediano ({_pen(m['median'])}).")
    reasons.append(f"El veredicto se basa en {m['n_awards']:,} compras de {m['year_from']}–{m['year_to']}.")
    if e["confidence"] == "high":
        reasons.append("Confianza alta: hay muchas compras y los precios son parecidos entre sí.")
    elif e["dispersion_level"] == "high":
        reasons.append("Confianza baja: los precios pagados varían mucho; compare con las compras de su zona.")
    else:
        reasons.append("Confianza media: hay pocas compras o los precios varían de forma moderada.")
    if m.get("pct_single_bidder") is not None and m["pct_single_bidder"] >= 25:
        reasons.append(f"En {m['pct_single_bidder']:.0f}% de los procesos se presentó un solo postor: hay espacio para nuevos proveedores.")

    places = []
    if detail:
        for p in detail["places"][:6]:
            places.append({**p, "margin": 100 * (p["median"] - e["cost"]) / e["cost"]})
        local = next((p for p in detail["places"] if p["local"]), None)
        if local and all(not p["local"] for p in places):
            places.append({**local, "margin": 100 * (local["median"] - e["cost"]) / e["cost"]})
    return {"title": title, "text": text, "reasons": reasons, "places": places}