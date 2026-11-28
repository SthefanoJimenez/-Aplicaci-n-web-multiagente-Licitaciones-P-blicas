"""Consulta básica de RUC. Solo se usan los campos necesarios para el perfil."""
from dataclasses import dataclass
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class RucDetails:
    ruc: str
    company_name: str
    status: str
    condition: str


class LookupError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    # Un redirect no debe reenviar el token a otro destino.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect())


def _unavailable():
    return LookupError(503, "La consulta de RUC no está disponible en este momento.")


def lookup(token, ruc):
    if not re.fullmatch(r"[0-9]{11}", ruc):
        raise LookupError(400, "Escriba un RUC de 11 dígitos.")
    request = Request(f"https://miapi.cloud/v1/ruc/{ruc}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
    })
    try:
        with _opener.open(request, timeout=5) as response:
            body = response.read(32769)
        if len(body) > 32768:
            raise _unavailable()
        data = json.loads(body)
    except HTTPError as exc:
        exc.close()
        if exc.code in (404, 422):
            raise LookupError(404, "No se encontraron datos para este RUC.") from None
        if exc.code == 429:
            raise LookupError(429, "Se alcanzó el límite de consultas. Intente más tarde.") from None
        raise _unavailable() from None
    except (URLError, OSError, ValueError, RecursionError):
        raise _unavailable() from None
    fields = data.get("datos") if isinstance(data, dict) and data.get("success") is True else None
    if not isinstance(fields, dict) or fields.get("ruc") != ruc:
        raise _unavailable()
    strings = [fields.get(field) for field in ("razon_social", "estado", "condicion")]
    if any(not isinstance(value, str) or not low <= len(value.strip()) <= high
           for value, low, high in zip(strings, (2, 1, 1), (200, 64, 64))):
        raise _unavailable()
    return RucDetails(ruc, *(value.strip() for value in strings))
