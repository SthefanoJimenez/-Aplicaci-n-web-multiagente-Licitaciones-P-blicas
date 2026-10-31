"""OE7: % de consultas de prueba que completan el flujo completo sin errores, en la aplicación desplegada.

Recorre el flujo como lo haría un usuario, por HTTP, con las mismas 30 consultas de HU-001
(tests/queries_hu001.csv): describir el bien -> elegir el primer código sugerido -> ver el
mercado -> ingresar un costo de prueba -> veredicto.

Una consulta «completa el flujo» si llega a su pantalla final sin error:
  - veredicto, si el bien tiene evidencia suficiente;
  - «Sin evidencia para evaluar» o el mercado sin veredicto, si no la tiene
    (es la respuesta correcta del sistema, no un error).
Es un error: código HTTP distinto de 200, la pantalla «El buscador del catálogo no respondió»
(el modelo no respondió) o una página inesperada.

Cada consulta deja su traza en LangSmith (clasificador y analista), que sirve como evidencia
independiente. El costo de prueba no se traza: el evaluador no usa LangSmith.

Uso (desde app/):
    python tests/run_flow_oe7.py https://SU-APP.onrender.com
Salida: tests/results_oe7.csv
"""
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

TESTS_DIR = Path(__file__).parent
PAUSE_SECONDS = 8          # igual que run_queries_hu001.py: respeta el límite de la capa gratuita de Gemini
TEST_COST = "10"           # costo ficticio: solo sirve para llegar al veredicto
TIMEOUT = 90               # Render (plan gratuito) puede tardar en despertar

base = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
if not base.startswith("http"):
    sys.exit("Indique la URL de la aplicación desplegada, por ejemplo: python tests/run_flow_oe7.py https://xxx.onrender.com")


def page(html):
    """Qué pantalla devolvió la aplicación."""
    if 'id="verdict-title"' in html:
        return "veredicto"
    if "Sin evidencia para evaluar" in html:
        return "sin_evidencia"
    if "El buscador del catálogo no respondió" in html:
        return "error_modelo"
    if "Elija el bien que mejor representa" in html:
        return "candidatos"
    if "El mercado de su bien" in html:
        return "mercado"
    return "desconocida"


def run_query(description):
    s = requests.Session()
    steps = []

    def step(name, response):
        steps.append(f"{name}:{response.status_code}")
        return response

    r = step("inicio", s.get(f"{base}/evaluar", timeout=TIMEOUT))
    r = step("describir", s.post(f"{base}/candidatos", data={"description": description}, timeout=TIMEOUT))
    screen = page(r.text)
    if r.status_code != 200 or screen != "candidatos":
        return screen, None, steps, r.status_code == 200 and screen == "sin_evidencia"

    codes = re.findall(r'name="code" value="(\d+)"', r.text)
    code = codes[0]
    r = step("confirmar", s.post(f"{base}/confirmar", data={"code": code}, timeout=TIMEOUT))
    screen = page(r.text)
    if r.status_code != 200 or screen != "mercado":
        return screen, code, steps, r.status_code == 200 and screen == "sin_evidencia"
    if "Ingresar mi costo" not in r.text:
        return "mercado_sin_veredicto", code, steps, True   # evidencia insuficiente: fin correcto del flujo

    r = step("costo", s.get(f"{base}/costo", timeout=TIMEOUT))
    r = step("veredicto", s.post(f"{base}/veredicto", data={"cost": TEST_COST, "rnp": "si"}, timeout=TIMEOUT))
    screen = page(r.text)
    return screen, code, steps, r.status_code == 200 and screen == "veredicto"


queries = list(csv.DictReader(open(TESTS_DIR / "queries_hu001.csv", encoding="utf-8-sig")))
rows = []
print(f"OE7 contra {base} — {datetime.now():%Y-%m-%d %H:%M}\n")
for q in queries:
    started = time.time()
    try:
        final, code, steps, ok = run_query(q["descripcion"])
        error = ""
    except requests.RequestException as e:
        final, code, steps, ok, error = "sin_respuesta", None, [], False, type(e).__name__
    seconds = time.time() - started
    rows.append({"id": q["id"], "tipo": q["tipo"], "descripcion": q["descripcion"], "codigo_elegido": code or "",
                 "pantalla_final": final, "pasos": " ".join(steps), "segundos": f"{seconds:.1f}",
                 "completa_sin_error": ok, "error": error, "fecha": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
    print(f"{q['id']:>2} {'✔' if ok else '✘'} {final:<22} {seconds:5.1f}s  {q['descripcion']}")
    time.sleep(PAUSE_SECONDS)

with open(TESTS_DIR / "results_oe7.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)

ok = sum(r["completa_sin_error"] for r in rows)
print(f"\nOE7: {ok}/{len(rows)} consultas completaron el flujo sin errores ({100 * ok / len(rows):.1f} %)")
print(f"Detalle: {TESTS_DIR / 'results_oe7.csv'}")