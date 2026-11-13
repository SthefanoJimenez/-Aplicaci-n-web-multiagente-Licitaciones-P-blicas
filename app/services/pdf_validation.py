"""Validador HU-015 aislado: stdin PDF, stdout número de páginas; nunca imprime el PDF."""
import io
import sys


MAX_MEMORY = 512 * 1024 * 1024
MAX_CPU_SECONDS = 6


def limit_resources(platform=None, resource=None):
    """Límites del proceso que revisa el PDF. Cada uno es opcional y su falla no rechaza el PDF:
    Windows no tiene el módulo resource y macOS no permite limitar el espacio de direcciones
    (RLIMIT_AS), así que ahí solo queda el de CPU. En todos los casos el proceso padre mantiene
    el tiempo máximo (8 s) y el tamaño máximo del archivo (5 MB)."""
    platform = platform or sys.platform
    if resource is None:
        try:
            import resource
        except ImportError:
            return []
    limits = [("cpu", resource.RLIMIT_CPU, MAX_CPU_SECONDS)]
    if platform != "darwin":
        limits.insert(0, ("memoria", resource.RLIMIT_AS, MAX_MEMORY))
    applied = []
    for name, which, value in limits:
        try:
            resource.setrlimit(which, (value, value))
            applied.append(name)
        except (ValueError, OSError):
            pass
    return applied


def main():
    limit_resources()
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