"""Linea base reproducible del asistente autenticado, con datos ficticios.

Reutiliza scripts/demostrar_hu010.py sin modificarlo. Sustituye Flask.run solo
en este proceso por un cliente de pruebas. No abre puertos ni usa Neon/Gemini.
No equivale a medir Render, Neon frio ni una arquitectura con/sin RAG.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import runpy
import statistics
import subprocess
import sys
import time

QUESTIONS = [
    ("Q01", "Evalua la convocatoria 9100101", "verde"),
    ("Q02", "Cual es el monto de la convocatoria 9100101?", "verde"),
    ("Q03", "Que garantia orientativa corresponde a la convocatoria 9100101?", "verde"),
    ("Q04", "Que documentos se consideran para la convocatoria 9100101?", "verde"),
    ("Q05", "Que requisito decide el color de la convocatoria 9100101?", "verde"),
    ("Q06", "Evalua la convocatoria 9100102", "amarillo"),
    ("Q07", "Por que falta el monto de la convocatoria 9100102?", "amarillo"),
    ("Q08", "Evalua la convocatoria 9100103", "rojo"),
    ("Q09", "Que experiencia exige la convocatoria 9100103?", "rojo"),
    ("Q10", "Que informacion necesito preparar?", "guia_general"),
]


def percentile90(values):
    return sorted(values)[max(0, math.ceil(0.9 * len(values)) - 1)]


def distribution(rows):
    values = [r["duracion_ms"] for r in rows if r["correcto"]]
    return {"n": len(values), "mediana_ms": round(statistics.median(values), 3) if values else None,
            "p90_ms": round(percentile90(values), 3) if values else None}


def measure(app, args, root, setup_start):
    import auth
    from services import accounts, assistant, eligibility_store, llm
    calls = {"llm": 0, "embeddings": 0}
    last = {}
    original_answer = assistant.ask_assistant
    originals = {name: getattr(llm, name) for name in ("generate_json", "embed_texts", "_get_client")}

    def blocked_llm(*a, **kw):
        calls["llm"] += 1
        raise RuntimeError("La medicion local bloquea las llamadas externas al modelo")

    def blocked_embed(*a, **kw):
        calls["embeddings"] += 1
        raise RuntimeError("La medicion local bloquea las llamadas externas de embeddings")

    def observed_answer(*a, **kw):
        result = original_answer(*a, **kw)
        last["result"] = result
        return result

    llm.generate_json = llm._get_client = blocked_llm
    llm.embed_texts = blocked_embed
    assistant.ask_assistant = observed_answer
    records = []
    try:
        app.config.update(TESTING=True)
        client = app.test_client()
        client.get("/ingresar")
        with client.session_transaction() as session:
            token = session["csrf_token"]
        login = client.post("/ingresar", data={"email": "demo-hu010@example.test",
                            "password": "Demo_hu010_2026!", "csrf_token": token})
        if login.status_code != 302:
            raise RuntimeError("No se pudo iniciar sesion en la demostracion temporal")
        with app.app_context():
            factory = auth.store()
            uid = accounts.authenticate(factory, "demo-hu010@example.test", "Demo_hu010_2026!")
        setup_ms = round((time.perf_counter() - setup_start) * 1000, 3)
        for repetition in range(1, args.repeticiones + 1):
            for question_id, query, expected in QUESTIONS:
                with app.app_context():
                    eligibility_store.clear_chat(factory, uid)
                client.get("/asistente", follow_redirects=True)
                with client.session_transaction() as session:
                    token = session["csrf_token"]
                last.clear()
                before = dict(calls)
                start = time.perf_counter()
                response = client.post("/asistente/mensaje", data={"query": query, "csrf_token": token})
                duration = round((time.perf_counter() - start) * 1000, 3)
                result = last.get("result", {})
                colors = [e["semaphore"] for e in result.get("evaluations", [])]
                output = (result.get("response") or "").lower()
                ok = (expected in colors) if expected != "guia_general" else (not colors and "tener listo" in output)
                llm_calls = calls["llm"] - before["llm"]
                embeddings = calls["embeddings"] - before["embeddings"]
                ok = bool(ok and response.status_code == 200 and not embeddings)
                records.append({"pregunta_id": question_id, "repeticion": repetition, "consulta": query,
                    "estado": "primera_consulta_del_proceso" if not records else "consulta_posterior",
                    "duracion_ms": duration, "http": response.status_code, "correcto": ok,
                    "colores": colors, "intentos_llm_bloqueados": llm_calls, "intentos_embedding_bloqueados": embeddings,
                    "llamadas_llm_externas": 0, "tokens_entrada": 0, "tokens_salida": 0,
                    "nota_tokens": "Modelo bloqueado por el ensayo: respuesta determinista de respaldo. No mide consumo con Gemini."})
                print(f"{question_id} repeticion {repetition}: {duration:.3f} ms; correcto={ok}", flush=True)
        try:
            commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
                                              stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            commit = "no disponible"
        args.salida.mkdir(parents=True, exist_ok=True)
        summary = distribution(records)
        summary["por_pregunta"] = {qid: distribution([r for r in records if r["pregunta_id"] == qid])
                                    for qid, _, _ in QUESTIONS}
        payload = {"fecha_utc": datetime.now(timezone.utc).isoformat(), "commit": commit,
            "python": platform.python_version(), "sistema": platform.system(),
            "arquitectura": "LangGraph del widget con redaccion determinista de respaldo; modelo bloqueado por el ensayo", "entorno": "SQLite temporal / Flask test_client / datos ficticios",
            "incluye": "POST /asistente/mensaje y respuesta JSON del widget; historial reiniciado antes de cada muestra",
            "excluye": "navegador, red HTTP real, arranque de Render, Neon, Gemini y comparacion con/sin RAG",
            "criterio_correcto": "HTTP 200 y color esperado; o cabecera de la guia general. No puntua la calidad completa de la respuesta.",
            "preparacion_ms": setup_ms, "p90_metodo": "rango mas cercano: ordenar y tomar ceil(0.9*n)",
            "demo_sha256": hashlib.sha256((root / "scripts/demostrar_hu010.py").read_bytes()).hexdigest(),
            "resumen": summary, "registros": records}
        (args.salida / "rn003_local.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# RN-003: linea base local", "", f"Fecha UTC: {payload['fecha_utc']}", f"Commit: {commit}",
            f"Entorno: {payload['entorno']}", "", f"Muestras correctas: {summary['n']}/{len(records)}.",
            f"Mediana: {summary['mediana_ms']} ms. P90: {summary['p90_ms']} ms.",
            "P90 por rango mas cercano. La preparacion y el login se excluyen del tiempo por consulta.", "",
            "| Pregunta | Muestras correctas | Mediana ms | P90 ms |", "|---|---:|---:|---:|"]
        for qid, stats in summary["por_pregunta"].items():
            lines.append(f"| {qid} | {stats['n']} | {stats['mediana_ms']} | {stats['p90_ms']} |")
        lines += ["", "Las llamadas conocidas a Gemini y embeddings se bloquearon en este proceso. El chat utilizo su redaccion determinista de respaldo.",
            "Tokens consumidos por modelos externos: 0, porque los intentos se interceptaron. No representan un ensayo con/sin RAG ni predicen su consumo real.",
            "La primera consulta del proceso no acredita un arranque frio de Neon.",
            "Esta linea base aporta evidencia parcial. RN-003 sigue pendiente de la comparacion solicitada y las mediciones de despliegue."]
        (args.salida / "rn003_local.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nMuestras correctas: {summary['n']}/{len(records)}. Mediana {summary['mediana_ms']} ms; P90 {summary['p90_ms']} ms.")
        print("Archivos: rn003_local.json y rn003_local.md. Es una linea base local, no el cierre completo de RN-003.")
        if summary["n"] != len(records):
            raise RuntimeError("Hubo muestras incorrectas. Revise el JSON; no presente la medicion como aprobada")
    finally:
        assistant.ask_assistant = original_answer
        for name, value in originals.items():
            setattr(llm, name, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repositorio", required=True, type=Path)
    parser.add_argument("--salida", required=True, type=Path)
    parser.add_argument("--repeticiones", type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    root = args.repositorio.resolve()
    demo = root / "scripts/demostrar_hu010.py"
    if not demo.is_file():
        parser.error("El repositorio debe contener scripts/demostrar_hu010.py")
    if "app" in sys.modules or "config" in sys.modules:
        parser.error("Ejecute el archivo como proceso independiente")
    from flask import Flask
    original_run = Flask.run
    started = time.perf_counter()
    invoked = []

    def bench_instead_of_server(app, *a, **kw):
        invoked.append(True)
        measure(app, args, root, started)

    Flask.run = bench_instead_of_server
    try:
        runpy.run_path(str(demo), run_name="__main__")
        if not invoked:
            raise RuntimeError("La demostracion no llego a inicializarse")
    finally:
        Flask.run = original_run


if __name__ == "__main__":
    main()
