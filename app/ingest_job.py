"""Tarea de la ingesta diaria (EN-007). La ejecuta GitHub Actions; también se puede correr a mano.

Uso, desde la carpeta app/ (con DATABASE_URL en el entorno o en app/.env):
    python ingest_job.py
    python ingest_job.py --diagnostico    solo consulta la API (sin base de datos) para ver si
                                          un 403 es un mes aún no publicado o un bloqueo

Crea las tablas que falten, ejecuta la ingesta y escribe el resumen en pantalla (y en el
resumen de la ejecución de GitHub Actions). Termina con código 1 si la ejecución falló, para
que el historial de GitHub Actions la muestre en rojo; los datos anteriores se conservan.
Nunca imprime la cadena de conexión ni las claves.
"""
import os
import sys

import config
from services import db, ingest

MESSAGES = {
    403: "El portal rechazó la consulta (403). Ejecute `python ingest_job.py --diagnostico` en GitHub Actions "
         "(el paso de diagnóstico corre solo cuando la ingesta falla) y en una computadora local: si /files "
         "responde 200 en la computadora y 403 en GitHub Actions con el mismo nombre de cliente, el portal "
         "bloquea la red de GitHub, no el User-Agent. La app sigue mostrando la última actualización.",
}


def scrub(text):
    """Quita de un mensaje la cadena de conexión y su contraseña, por si el error las incluyera."""
    from urllib.parse import urlsplit
    text = str(text)
    if config.DATABASE_URL:
        text = text.replace(config.DATABASE_URL, "[DATABASE_URL]")
        password = urlsplit(config.DATABASE_URL).password
        if password:
            text = text.replace(password, "[contraseña]")
    return text


def client_kind():
    return "navegador" if config.OCDS_USER_AGENT.startswith("Mozilla/5.0 (Macintosh") else "personalizado"


def run_diagnosis():
    """Consultas pequeñas a la API (sin base de datos) y su resultado en pantalla y en el resumen."""
    from services import ocds_source
    lines = [f"Diagnóstico de la API OCDS · nombre de cliente: {client_kind()} · "
             f"origen: {os.environ.get('GITHUB_EVENT_NAME') or 'local'}"]
    results = ocds_source.diagnose()
    for label, status, detail in results:
        lines.append(f"- {label}: {status} · {detail}")
    files_status = results[0][1]
    month_status = [status for _label, status, _detail in results[1:]]
    if files_status == 403:
        lines.append("Conclusión: /files también responde 403, así que no es un mes sin publicar. "
                     "Si en una computadora local responde 200, el portal bloquea esta red (por ejemplo, la de GitHub).")
    elif files_status == 200 and 200 in month_status and any(s in (403, 404) for s in month_status):
        lines.append("Conclusión: la API responde a la app desde aquí (/files y un mes con 200). El mes con 403/404 "
                     "no figura en /files: aún no está publicado. Compare con el diagnóstico de GitHub Actions: "
                     "si allí /files da 403, el portal bloquea la red de GitHub, no a la app.")
    elif files_status == 200 and all(s == 200 for s in month_status):
        lines.append("Conclusión: la API responde con normalidad desde aquí.")
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write("### Diagnóstico de la API\n\n" + "\n".join(lines) + "\n")
    return 0


def main_with(args):
    """Como main(), con los argumentos explícitos (lo usan las pruebas)."""
    return main(list(args))


def main(args=None):
    args = sys.argv[1:] if args is None else args
    if "--diagnostico" in args:
        return run_diagnosis()
    if not config.DATABASE_URL:
        print("Falta DATABASE_URL (variable de entorno o app/.env). No se ejecutó la ingesta.")
        return 2
    engine = db.make_engine()
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    trigger = os.environ.get("GITHUB_EVENT_NAME") or "local"
    client = client_kind()
    print(f"Ingesta · meses: {config.OCDS_MONTHS} · rubros: {', '.join(config.PILOT_RUBROS)} · "
          f"nombre de cliente: {client} · origen: {trigger}")
    result = ingest.run_ingestion(factory, trigger=trigger)

    if result["status"] == "ok":
        lines = [
            f"Ejecución {result['run_id']}: correcta.",
            f"Bienes vigentes en la fuente: {result['source_open_goods']:,} · de los rubros piloto: {result['source_open_pilot']:,}",
            f"Vigentes guardadas: {result['saved_open']:,} · nuevas: {result['new']:,} · actualizadas: "
            f"{result['updated']:,} · pasaron a no vigentes: {result['closed']:,} · documentos nuevos: {result['documents_added']:,}",
        ]
        for month, reason in result.get("skipped_months", []):
            lines.append(f"Mes {month} omitido: {reason}. Se volverá a intentar en la próxima ejecución.")
    else:
        lines = [f"Ejecución {result['run_id']}: FALLIDA. Se conservan los datos anteriores.",
                 f"Motivo: {scrub(result['error'])}"]
        if result.get("http_status") in MESSAGES:
            lines.append(MESSAGES[result["http_status"]])
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write("### Ingesta diaria de convocatorias\n\n" + "\n\n".join(lines) + "\n")
    engine.dispose()
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())