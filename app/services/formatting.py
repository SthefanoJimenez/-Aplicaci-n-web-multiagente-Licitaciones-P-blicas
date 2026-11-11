"""Formatos de presentación compartidos por las pantallas y el agente redactor."""

import html
import re

UNIT_TEXT = {"M3": "m³", "M2": "m²", "Pie 2": "pie²", "Galon": "galón"}


def format_pen(value):
    """Soles: S/ 1,234.56. Los precios menores a S/ 0.10 se muestran con 4 decimales."""
    decimals = 2 if value >= 0.1 else 4
    return f"S/ {value:,.{decimals}f}"


def format_pen_total(value):
    """Total monetario en soles, siempre con dos decimales (incluido cero)."""
    return f"S/ {value:,.2f}"


def format_quantity(value):
    """Cantidad sin símbolo monetario ni ceros decimales sobrantes."""
    text = format(value, ",f")
    return text.rstrip("0").rstrip(".") if "." in text else text


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


# ---------------------------------------------------------------------------
# Markdown mínimo para las respuestas del asistente (HU-010 / HU-014)
# ---------------------------------------------------------------------------
# El asistente redacta con encabezados, negritas, viñetas y citas. Antes se mostraba
# el texto tal cual, con los asteriscos y los ">" a la vista. Este filtro convierte ese
# subconjunto en HTML. Escapa el texto primero, así que no puede inyectar etiquetas.
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_RULE = re.compile(r"^-{3,}$")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def _inline(text):
    """Negritas, cursivas y código en línea sobre texto ya escapado."""
    text = html.escape(str(text), quote=False)
    text = _CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return _ITALIC.sub(r"<em>\1</em>", text)


def markdown(text):
    """Convierte el Markdown que usa el asistente en HTML seguro."""
    blocks, paragraph, bullets, quotes = [], [], [], []

    def flush():
        nonlocal paragraph, bullets, quotes
        if paragraph:
            blocks.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph = []
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in bullets) + "</ul>")
            bullets = []
        if quotes:
            blocks.append("<blockquote>" + "<br>".join(_inline(item) for item in quotes) + "</blockquote>")
            quotes = []

    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif _RULE.match(line):
            flush()
            blocks.append("<hr>")
        elif line.startswith(">"):
            if paragraph or bullets:
                flush()
            quotes.append(line.lstrip("> ").strip())
        elif _BULLET.match(line):
            if paragraph or quotes:
                flush()
            bullets.append(_BULLET.match(line).group(1))
        else:
            if bullets or quotes:
                flush()
            paragraph.append(line)
    flush()
    return "\n".join(blocks)
