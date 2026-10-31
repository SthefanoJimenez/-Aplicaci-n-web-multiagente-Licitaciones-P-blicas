"""API OCDS simulada para las pruebas de EN-007 (sin internet).

Sigue la forma de analisis/tests_sp005/fake_ocds_server.py del SP-005 (listado /files, archivo
mensual en ZIP con un paquete de releases y rechazo 403 a clientes sin identificar), pero corre
dentro del proceso de pytest y sin PDF, para no sumar dependencias a la app.
"""
import io
import json
import threading
import zipfile
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def release(n, now, family="15101505", category="goods", item_status="CONVOCADO", docs=True,
            dept="LA LIBERTAD", date=None):
    """Una convocatoria con la forma de los datos reales del OECE (estado en items[].statusDetails)."""
    published = (date or now - timedelta(days=2)).isoformat()
    documents = [
        {"id": "1", "title": "Bases Administrativas", "documentType": "biddingDocuments", "format": "pdf",
         "url": f"https://seace.example/{n}/bases.pdf"},
        {"id": "2", "title": "Resumen ejecutivo", "documentType": "tenderNotice", "format": "pdf",
         "url": f"https://seace.example/{n}/resumen.pdf"},
    ] if docs else []
    return {
        "ocid": f"ocds-dgv273-seacev3-{n}", "date": published,
        "buyer": {"id": f"PE-RUC-{n}", "name": f"ENTIDAD {n}"},
        "parties": [{"id": f"PE-RUC-{n}", "name": f"ENTIDAD {n}", "roles": ["buyer"], "address": {"department": dept}}],
        "tender": {
            "id": str(n), "title": f"ADQUISICION {n}", "mainProcurementCategory": category,
            "procurementMethodDetails": "Subasta Inversa Electrónica", "datePublished": published,
            "tenderPeriod": {"startDate": published, "endDate": published},
            "value": {"amount": 0, "currency": "PEN"},
            "items": [{"description": f"BIEN {n}", "statusDetails": item_status,
                       "classification": {"scheme": "UNSPSC", "id": family}}],
            "documents": documents,
        },
    }


def monthly_zip(releases):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("seace_v3.json", json.dumps({"uri": "x", "version": "1.1", "releases": releases}))
    return buffer.getvalue()


class FakeOCDS:
    """Servidor en un hilo. months = {(año, mes): [releases]}; fail = código HTTP para el archivo mensual."""

    def __init__(self):
        self.months = {}
        self.fail = None
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                agent = self.headers.get("User-Agent", "")
                fake.requests.append((self.path, agent))
                if agent.startswith(("curl/", "python-requests/")):
                    return self._send(403, b"", "text/plain")   # como el portal real con clientes sin identificar
                if self.path.startswith("/api/v1/files"):
                    results = [{"source": "seace_v3", "year": str(y), "month": f"{m:02d}",
                                "files": {"json": f"{fake.base}/api/v1/file/seace_v3/json/{y}/{m:02d}/"}}
                               for (y, m) in fake.months] if "page=1" in self.path else []
                    return self._send(200, json.dumps({"results": results}).encode(), "application/json")
                if self.path.startswith("/api/v1/file/seace_v3/json/"):
                    if fake.fail:
                        return self._send(fake.fail, b"", "text/plain")
                    year, month = self.path.rstrip("/").split("/")[-2:]
                    body = monthly_zip(fake.months.get((int(year), int(month)), []))
                    return self._send(200, body, "application/zip")
                return self._send(404, b"", "text/plain")

            def _send(self, status, body, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()