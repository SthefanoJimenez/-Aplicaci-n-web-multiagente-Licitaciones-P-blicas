"""Formato limitado del chat: texto escapado, títulos, listas y énfasis.

No admite HTML, imágenes ni enlaces del mensaje como elementos activos.
"""
import re
from html import escape

from markupsafe import Markup


INLINE = re.compile(r"\*\*([^\n]+?)\*\*|(?<!\*)\*([^*\n]+)\*(?!\*)|`([^`\n]+)`")


def _inline(text: str) -> str:
    parts = []
    position = 0
    for match in INLINE.finditer(text):
        parts.append(escape(text[position:match.start()]))
        for index, tag in ((1, "strong"), (2, "em"), (3, "code")):
            if match.group(index) is not None:
                parts.append(f"<{tag}>{escape(match.group(index))}</{tag}>")
                break
        position = match.end()
    parts.append(escape(text[position:]))
    return "".join(parts)


def render_assistant_text(value: str) -> Markup:
    """Genera únicamente etiquetas propias y escapa todo el contenido recibido."""
    output = []
    paragraph = []
    list_tag = None

    def flush_paragraph():
        if paragraph:
            output.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            output.append(f"</{list_tag}>")
            list_tag = None

    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        heading = re.fullmatch(r"(#{3,4})\s+(.+)", line)
        item = re.fullmatch(r"(?:[-*]|\d+[.)])\s+(.+)", line)
        if not line:
            flush_paragraph()
            close_list()
        elif item:
            flush_paragraph()
            tag = "ol" if line[0].isdigit() else "ul"
            if tag != list_tag:
                close_list()
                output.append(f"<{tag}>")
                list_tag = tag
            output.append("<li>" + _inline(item.group(1)) + "</li>")
        else:
            close_list()
            if heading:
                flush_paragraph()
                tag = "h" + str(len(heading.group(1)))
                output.append(f"<{tag}>" + _inline(heading.group(2)) + f"</{tag}>")
            elif line == "---":
                flush_paragraph()
                output.append("<hr>")
            elif line.startswith("> "):
                flush_paragraph()
                output.append("<blockquote>" + _inline(line[2:]) + "</blockquote>")
            else:
                paragraph.append(_inline(line))
    flush_paragraph()
    close_list()
    return Markup("\n".join(output))
