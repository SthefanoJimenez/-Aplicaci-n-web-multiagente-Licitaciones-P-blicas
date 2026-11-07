"""Validador HU-015 aislado: stdin PDF, stdout número de páginas; nunca imprime el PDF."""
import io
import sys


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    except ImportError:  # Windows: el padre mantiene el límite de tiempo y tamaño.
        pass
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject
    content = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        return 1
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted:
        return 1
    if not 1 <= len(reader.pages) <= 200:
        return 1
    pending, seen = [reader.trailer], set()
    count = 0
    while pending:
        item = pending.pop()
        if isinstance(item, IndirectObject):
            key = (item.idnum, item.generation)
            if key in seen:
                continue
            seen.add(key)
            item = item.get_object()
        count += 1
        if count > 100000:
            return 1
        if isinstance(item, DictionaryObject):
            if set(item) & {"/JavaScript", "/JS", "/OpenAction", "/AA", "/EmbeddedFiles", "/EF", "/XFA", "/RichMediaContent"}:
                return 1
            if str(item.get("/S", "")) in {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/Rendition"}:
                return 1
            if str(item.get("/Type", "")) == "/EmbeddedFile":
                return 1
            pending.extend(item.values())
        elif isinstance(item, ArrayObject):
            pending.extend(item)
    print(len(reader.pages))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(1)
