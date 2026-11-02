"""Descarga de las bases a través de la app (HU-007, corrección del 01/11/2026).

Problema: el botón "Descargar bases" enlazaba directo a la dirección que publica el OECE
(prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=…). Ese servidor responde
application/octet-stream sin un nombre de archivo confiable, así que el navegador lo guarda con
una extensión que no corresponde y el archivo "no abre" o aparece como ZIP corrupto. Además, el
formato real varía: en la muestra del SP-005 hubo PDF, DOCX, ZIP y RAR.

Solución: la app pide el archivo, mira sus primeros bytes para saber qué es realmente y lo
entrega con el tipo y la extensión correctos (bases_<código>.pdf, .docx, .zip, .rar, .7z).
Si lo que llega no es un archivo reconocible (por ejemplo, una página HTML de error), no se
entrega como descarga: la ruta muestra un aviso con el enlace original del SEACE.

Solo se piden direcciones https de dominios del Estado (seace.gob.pe, oece.gob.pe) que vienen
de la base de convocatorias, nunca una dirección escrita por el usuario.
"""
from dataclasses import dataclass
from typing import Iterator, Optional
from urllib.parse import urlsplit

import requests

import config

ALLOWED_DOMAINS = ("seace.gob.pe", "oece.gob.pe")
MAX_BYTES = 80 * 1024 * 1024          # la base más grande del SP-005 pesó ~28 MB
TIMEOUT_SECONDS = (15, 60)            # (conectar, leer)
CHUNK = 256 * 1024
HEAD_BYTES = 8192                     # en los DOCX del SP-005, "word/" aparece hacia el byte 1,941

# (extensión, tipo MIME, nombre para mostrar)
KINDS = {
    "pdf": ("pdf", "application/pdf", "PDF"),
    "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word"),
    "doc": ("doc", "application/msword", "Word"),
    "zip": ("zip", "application/zip", "ZIP"),
    "rar": ("rar", "application/vnd.rar", "RAR"),
    "7z": ("7z", "application/x-7z-compressed", "7z"),
    "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Excel"),
}


class BasesUnavailable(Exception):
    """No se pudo obtener un archivo válido. reason: texto corto para el aviso."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass
class BasesFile:
    kind: str                      # clave de KINDS
    size: Optional[int]            # bytes, si el servidor lo informa
    chunks: Iterator[bytes]        # contenido completo, empezando por los bytes ya leídos

    @property
    def extension(self):
        return KINDS[self.kind][0]

    @property
    def mimetype(self):
        return KINDS[self.kind][1]


def is_allowed(url):
    """Solo https y dominios del Estado: evita que la app pida direcciones arbitrarias."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def detect_kind(head):
    """Formato real según los primeros bytes; None si no es un archivo reconocible.

    Un DOCX o XLSX es un ZIP: se distinguen por los nombres internos que aparecen al inicio
    del archivo (word/ o xl/). Si no se ven, se trata como ZIP.
    """
    if head[:5] == b"%PDF-" or head[:4] == b"%PDF":
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        if b"word/" in head:
            return "docx"
        if b"xl/" in head:
            return "xlsx"
        return "zip"
    if head[:6] == b"Rar!\x1a\x07":
        return "rar"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":     # Word 97-2003 (.doc)
        return "doc"
    return None


def open_bases(url, session=None):
    """Pide el archivo y devuelve BasesFile listo para enviarlo; lanza BasesUnavailable si no sirve."""
    if not is_allowed(url):
        raise BasesUnavailable("el enlace no es de un dominio del SEACE")
    session = session or requests.Session()
    headers = {"User-Agent": config.OCDS_USER_AGENT, "Accept": "*/*", "Accept-Language": "es-PE,es;q=0.9"}
    try:
        response = session.get(url, headers=headers, timeout=TIMEOUT_SECONDS, stream=True, allow_redirects=True)
    except requests.RequestException as e:
        raise BasesUnavailable(f"el SEACE no respondió ({type(e).__name__})") from e
    if response.status_code != 200:
        response.close()
        raise BasesUnavailable(f"el SEACE respondió {response.status_code}")
    if not is_allowed(response.url):
        response.close()
        raise BasesUnavailable("el SEACE redirigió a un dominio no permitido")
    size = response.headers.get("Content-Length")
    size = int(size) if size and size.isdigit() else None
    if size is not None and size > MAX_BYTES:
        response.close()
        raise BasesUnavailable("el archivo es demasiado grande para pasar por la app")
    if (response.headers.get("Content-Encoding") or "identity").lower() != "identity":
        size = None   # requests descomprime: el tamaño informado no es el que se enviará

    raw = response.iter_content(CHUNK)
    head = b""
    try:
        while len(head) < HEAD_BYTES:
            piece = next(raw)
            if not piece:
                continue
            head += piece
    except StopIteration:
        pass
    except requests.RequestException as e:
        response.close()
        raise BasesUnavailable(f"la descarga se cortó ({type(e).__name__})") from e
    kind = detect_kind(head)
    if kind is None:
        response.close()
        raise BasesUnavailable("el SEACE no entregó un archivo (respondió una página u otro contenido)")

    def chunks():
        sent = len(head)
        try:
            yield head
            for piece in raw:
                sent += len(piece)
                if sent > MAX_BYTES:
                    break
                yield piece
        finally:
            response.close()

    return BasesFile(kind=kind, size=size, chunks=chunks())


def label(kind):
    return KINDS[kind][2] if kind in KINDS else None


def declared_label(doc_format):
    """Formato que declara el OECE (puede no coincidir con el real), para el texto del botón."""
    key = str(doc_format or "").strip().lower().lstrip(".")
    return label(key)