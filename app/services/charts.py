"""Geometría de los gráficos SVG que se dibujan en el servidor (sin librerías de gráficos).

Así las pantallas funcionan aunque el navegador no cargue JavaScript; el JavaScript de
app.js solo agrega la ventana con el valor al pasar el cursor.
"""
import json
import math
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _nice_max(value):
    """Máximo «redondo» para el eje: 1, 2, 2.5, 5 o 10 por potencia de diez."""
    if value <= 0:
        return 1
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= step * power:
            return step * power
    return 10 * power


def area_chart(points, width=760, height=240, pad_left=48, pad_bottom=28, pad_top=12, pad_right=8):
    """Serie mensual como línea con área.

    points: lista de {"label", "n", ...}. Devuelve las rutas, las marcas del eje y una zona
    sensible por punto para la ventana con el valor.
    """
    if not points:
        return None
    plot_w, plot_h = width - pad_left - pad_right, height - pad_top - pad_bottom
    top = _nice_max(max(p["n"] for p in points))
    step = plot_w / max(len(points) - 1, 1)

    def x(i):
        return round(pad_left + i * step, 1)

    def y(v):
        return round(pad_top + plot_h * (1 - v / top), 1)

    coords = [(x(i), y(p["n"])) for i, p in enumerate(points)]
    line = "M" + " L".join(f"{a} {b}" for a, b in coords)
    base = pad_top + plot_h
    area = f"{line} L{coords[-1][0]} {base} L{coords[0][0]} {base}Z"
    ticks = [{"y": y(top * f), "label": f"{top * f:,.0f}"} for f in (0, 0.25, 0.5, 0.75, 1)]
    # Etiquetas del eje X: enero de cada año (o cada 3 meses si hay pocos puntos)
    every = 1 if len(points) <= 12 else None
    x_labels = [{"x": x(i), "label": p["label"] if every else p["label"][-4:]}
                for i, p in enumerate(points)
                if (every and i % 2 == 0) or (not every and p["label"].startswith("ene"))]
    hits = [{"x": round(x(i) - step / 2, 1), "w": round(step, 1), "cx": x(i), "cy": y(p["n"]), "point": p}
            for i, p in enumerate(points)]
    peak = max(range(len(points)), key=lambda i: points[i]["n"])
    return {"width": width, "height": height, "line": line, "area": area, "ticks": ticks,
            "x_labels": x_labels, "hits": hits, "base": base, "left": pad_left, "right": width - pad_right,
            "peak": {"x": coords[peak][0], "y": coords[peak][1], "point": points[peak]}}


@lru_cache(maxsize=1)
def peru_map():
    """Polígonos de los departamentos ya proyectados (ver data/peru_departamentos.json)."""
    with open(DATA_DIR / "peru_departamentos.json", encoding="utf-8") as f:
        return json.load(f)