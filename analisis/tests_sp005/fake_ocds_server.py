"""Servidor simulado de la API OCDS para probar el script de SP-005 sin internet."""
import io, json, sys, zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

PORT = int(sys.argv[1])
BASE = f"http://127.0.0.1:{PORT}"
LIMA = timezone(timedelta(hours=-5))
now = datetime.now(LIMA)

def text_pdf():
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
    for page in range(6):
        y = 800
        lines = (["CAPITULO III REQUERIMIENTO"] + ["Especificaciones tecnicas del bien a contratar, linea %d de texto." % i for i in range(30)]
                 if page != 3 else ["REQUISITOS DE CALIFICACION", "A. CAPACIDAD LEGAL - Habilitacion",
                 "B. EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD: facturacion acumulada equivalente a S/ 150,000.00",
                 "por la venta de bienes iguales o similares al objeto de la convocatoria."] + ["detalle %d" % i for i in range(25)] + ["", "ANEXO 1 DECLARACION JURADA"])
        for line in lines:
            c.drawString(50, y, line); y -= 22
        c.showPage()
    c.save(); return buf.getvalue()

def scanned_pdf():
    pages = []
    for _ in range(3):
        img = Image.new("RGB", (600, 800), "white"); d = ImageDraw.Draw(img)
        for i in range(30): d.line((50, 40 + i * 24, 550, 40 + i * 24), fill="gray")
        pages.append(img)
    buf = io.BytesIO(); pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:]); return buf.getvalue()

FILES = {"/docs/texto.pdf": ("application/pdf", text_pdf()), "/docs/escaneado.pdf": ("application/pdf", scanned_pdf()),
         "/docs/html": ("text/html", b"<html>login</html>")}

def release(n, category, deadline_days, dept, family_items, docs, region_key="region"):
    deadline = (now + timedelta(days=deadline_days)).isoformat()
    return {"ocid": f"ocds-dgv273-seacev3-{n}", "date": now.isoformat(),
            "buyer": {"id": f"PE-RUC-{n}", "name": f"ENTIDAD {n}"},
            "parties": [{"id": f"PE-RUC-{n}", "name": f"ENTIDAD {n}", "roles": ["buyer"], "address": {region_key: dept}}],
            "tender": {"id": f"SIE-{n}-2026", "title": f"ADQUISICION {n}", "mainProcurementCategory": category,
                       "status": "active", "procurementMethodDetails": "Subasta Inversa Electrónica",
                       "tenderPeriod": {"startDate": (now - timedelta(days=5)).isoformat(), "endDate": deadline},
                       "value": {"amount": 1000.0 * n, "currency": "PEN"},
                       "items": [{"description": desc, "classification": {"scheme": "UNSPSC", "id": unspsc},
                                  "additionalClassifications": [{"scheme": "CUBSO", "id": unspsc + "0001", "description": desc}]}
                                 for unspsc, desc in family_items],
                       "documents": docs}}

cement = [("30111601", "CEMENTO PORTLAND TIPO I X 42.50 kg")]
fuel = [("15101505", "DIESEL B5 S-50")]
other = [("44121701", "LAPICERO DE TINTA SECA")]
bases = lambda path: [{"id": "1", "title": "Bases Administrativas", "documentType": "biddingDocuments", "format": "pdf", "url": BASE + path}]
releases = []
n = 1
for i in range(7):   # cemento: 7 vigentes, 2 en La Libertad, documentos variados
    path = ["/docs/texto.pdf", "/docs/escaneado.pdf", "/docs/no-existe.pdf", "/docs/html", "/docs/texto.pdf", "/docs/texto.pdf", None][i]
    releases.append(release(n, "goods", 3 + i, "LA LIBERTAD" if i < 2 else "LIMA", cement, bases(path) if path else [])); n += 1
for i in range(6):   # combustible: 6 vigentes, 1 en La Libertad
    releases.append(release(n, "goods", 4, "LA LIBERTAD" if i == 0 else "CUSCO", fuel, bases("/docs/texto.pdf"))); n += 1
for i in range(5):   # útiles: 5 vigentes, ninguna en La Libertad, sin catálogo
    releases.append(release(n, "goods", 2, "PIURA", other, bases("/docs/texto.pdf"))); n += 1
releases.append(release(n, "goods", -3, "LA LIBERTAD", cement, bases("/docs/texto.pdf"))); n += 1   # vencida
releases.append(release(n, "services", 5, "LA LIBERTAD", other, [])); n += 1                        # servicio
package = {"uri": "x", "version": "1.1", "releases": releases}
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z: z.writestr("seace_v3.json", json.dumps(package))
MONTH = buf.getvalue()

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/v1/files"):
            ctype = "application/json"
            month_url = f"{BASE}/descargas/seace_v3/json/{now.year}/{now.month:02d}/"
            page = 1 if "page=1" in self.path or "?" not in self.path else 2
            results = [{"id": f"seace_v3-{now.year}-{now.month:02d}", "source": "seace_v3", "year": str(now.year),
                        "month": f"{now.month:02d}", "files": {"csv": "x", "json": month_url}}] if page == 1 else []
            body = json.dumps({"results": results}).encode()
        elif self.path.startswith("/api/v1/file/seace_v3/json/") or self.path.startswith("/descargas/seace_v3/json/"):
            ctype, body = "application/zip", MONTH
        elif self.path.startswith("/api/v1/record/"):
            ocid = self.path.rsplit("/", 1)[-1]
            found = [r for r in releases if r["ocid"] == ocid]
            if not found:
                self.send_response(404); self.end_headers(); return
            ctype = "application/json"
            body = json.dumps({"records": [{"ocid": ocid, "releases": found, "compiledRelease": found[0]}]}).encode()
        elif self.path.startswith("/api/v1/release/seace_v3/"):
            tender_id = self.path.rsplit("/", 1)[-1]
            ctype = "application/json"
            body = json.dumps({"releases": [r for r in releases if r["tender"]["id"] == tender_id]}).encode()
        elif self.path in FILES:
            ctype, body = FILES[self.path]
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", PORT), H).serve_forever()