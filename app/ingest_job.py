"""Tarea de la ingesta diaria (EN-007). La ejecuta GitHub Actions; también se puede correr a mano.

Uso, desde la carpeta app/ (con DATABASE_URL en el entorno o en app/.env):
    python ingest_job.py

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
    403: "El portal rechazó la consulta (403). Revise OCDS_USER_AGENT y pruebe con "
         "analisis/sp006_04_recuperar_bases.py --probar-api. La app sigue mostrando la última actualización.",
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


def main():
    if not config.DATABASE_URL:
        print("Falta DATABASE_URL (variable de entorno o app/.env). No se ejecutó la ingesta.")
        return 2
    engine = db.make_engine()
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    trigger = os.environ.get("GITHUB_EVENT_NAME") or "local"
    client = "navegador" if config.OCDS_USER_AGENT.startswith("Mozilla/5.0 (Macintosh") else "personalizado"
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