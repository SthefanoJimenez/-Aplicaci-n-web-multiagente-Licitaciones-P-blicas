"""Formatos de presentación compartidos por las pantallas y el agente redactor."""

UNIT_TEXT = {"M3": "m³", "M2": "m²", "Pie 2": "pie²", "Galon": "galón"}


def format_pen(value):
    """Soles: S/ 1,234.56. Los precios menores a S/ 0.10 se muestran con 4 decimales."""
    decimals = 2 if value >= 0.1 else 4
    return f"S/ {value:,.{decimals}f}"


def format_amount(value):
    """Montos grandes en palabras cortas: S/ 245 mil, S/ 1.2 millones, S/ 8.3 mil millones."""
    if value >= 1_000_000_000:
        return f"S/ {value / 1_000_000_000:,.1f} mil millones"
    if value >= 1_000_000:
        millions = value / 1_000_000
        return f"S/ {millions:,.1f} millones" if millions < 100 else f"S/ {millions:,.0f} millones"
    if value >= 10_000:
        return f"S/ {value / 1000:,.0f} mil"
    return f"S/ {value:,.0f}"


def unit_text(unit):
    """Unidad de medida del catálogo escrita para una frase: "por galón", "por m³"."""
    return UNIT_TEXT.get(unit, str(unit).lower())