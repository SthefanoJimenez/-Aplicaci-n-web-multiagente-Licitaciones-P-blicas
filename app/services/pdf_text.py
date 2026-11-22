"""Lector aislado del texto de un PDF de la empresa (20/11/2026): stdin PDF, stdout texto.

Lo usa el personal clave para leer el título, el currículum o la constancia de habilidad que sube la
empresa. Corre en un proceso aparte, con los mismos límites que services/pdf_validation.py, porque el PDF
viene del usuario. Solo lee el texto de las primeras páginas; no envía nada a modelos ni trazas.
"""
import io
import sys

from pdf_validation import limit_resources

MAX_PAGES = 30
MAX_CHARS = 200_000


def main():
    limit_resources()
    from pypdf import PdfReader
    content = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        return 1
    reader = PdfReader(io.BytesIO(content))
    parts, size = [], 0
    for page in reader.pages[:MAX_PAGES]:
        try:
            text = page.extract_text() or ""
        except Exception:          # una página dañada no impide leer las demás
            text = ""
        parts.append(text)
        size += len(text)
        if size > MAX_CHARS:
            break
    sys.stdout.write("\n".join(parts)[:MAX_CHARS])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(1)