"""Tarea de la ingesta diaria (EN-007). La ejecuta GitHub Actions; también se puede correr a mano.

Uso, desde la carpeta app/ (con DATABASE_URL en el entorno o en app/.env):
    python ingest_job.py
    python ingest_job.py --diagnostico    solo consulta la API (sin base de datos) para ver si
                                          un 403 es un mes aún no publicado o un bloqueo
    python ingest_job.py --sin-rag        solo la ingesta, sin leer las bases
    python ingest_job.py --solo-rag       solo lee las bases pendientes (RAG, EN-012)
    python ingest_job.py --buscar "experiencia mínima" [--codigo 1258127]
                                          prueba la búsqueda en los fragmentos guardados
    python ingest_job.py --diagnosticar-bases [--codigo 1262871]
                                          por qué no se encontró la sección de requisitos
                                          (vuelve a descargar las bases; no guarda nada)
    python ingest_job.py --releer-sin-seccion
                                          vuelve a leer las bases "sin la sección" (tras mejorar el lector)

Crea las tablas que falten, ejecuta la ingesta y, si hay GEMINI_API_KEY, lee las bases de las
convocatorias nuevas y guarda su sección de requisitos con vectores (RAG). Escribe el resumen en
pantalla (y en el resumen de la ejecución de GitHub Actions). Termina con código 1 si la ejecución falló, para
que el historial de GitHub Actions la muestre en rojo; los datos anteriores se conservan.
Nunca imprime la cadena de conexión ni las claves.
"""
import argparse
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


def run_rag(factory):
    """Lee las bases pendientes y devuelve las líneas del resumen. No hace fallar la ingesta."""
    from services import rag
    if not config.GEMINI_API_KEY:
        return ["Lectura de bases (RAG) omitida: falta GEMINI_API_KEY para generar los vectores."]
    def show(number, total, code, status, seconds):
        print(f"  {number}/{total} · {code} · {status} · {seconds:.0f} s", flush=True)

    print("Leyendo bases pendientes…", flush=True)
    result = rag.index_pending(factory, progress=show)
    lines = [f"Lectura de bases (RAG): {result['processed']} procesadas · con requisitos: {result['extraido']} · "
             f"sin la sección: {result['seccion_no_encontrada']} · no legibles: {result['no_legible']} · "
             f"con error: {result['error']} · fragmentos: {result['chunks']:,} · tokens estimados: {result['tokens']:,}"
             f" · quedan por leer: {result['remaining']}"]
    if result["extraido"]:
        lines.append(f"Tokens estimados por base con requisitos: {result['tokens'] // result['extraido']:,}")
    if result["stopped"]:
        lines.append(f"Se detuvo la lectura de bases porque Gemini no respondió ({scrub(result['stopped'])}). "
                     "Las pendientes se leerán en la próxima ejecución.")
    for code, status, detail in result["details"]:
        if status != "extraido":
            lines.append(f"- {code}: {status} ({detail})")
    return lines


def run_search(factory, query, codes):
    from services import rag
    if not config.GEMINI_API_KEY:
        print("Falta GEMINI_API_KEY para convertir la pregunta en vector.")
        return 2
    with factory() as session:
        results = rag.search(session, query, k=5, codes=codes)
    if not results:
        print("No hay fragmentos guardados que coincidan. ¿Ya se ejecutó la lectura de bases?")
        return 1
    for r in results:
        page = f"pág. {r['page']}" if r["page"] else "Word"
        print(f"\n[{r['similarity']:.3f}] {r['code']} · {r['entity']} · {page} · {r['source']}")
        print("   " + r["text"][:400].replace("\n", " "))
    return 0


def run_bases_diagnosis(factory, codes):
    """Descarga las bases "sin la sección" (o las indicadas) y muestra por qué no se halló. No escribe nada."""
    from sqlalchemy import select
    from services import bases_download, bases_reader, rag
    with factory() as session:
        query = select(db.Tender).where(db.Tender.state == "vigente")
        query = (query.where(db.Tender.code.in_(codes)) if codes
                 else query.where(db.Tender.requirements_status == "seccion_no_encontrada"))
        tenders = []
        for t in session.scalars(query.order_by(db.Tender.code)):
            bases = next((d for d in t.documents if d.is_bases and d.url), None)
            others = [f"{d.title} ({d.doc_format})" for d in t.documents if d is not bases]
            tenders.append((t.code, t.title, bases.url if bases else None,
                            f"{bases.title} ({bases.doc_format})" if bases else None, others))
    if not tenders:
        print("No hay convocatorias vigentes sin la sección de requisitos.")
        return 0
    use_ocr = bases_reader.ocr_available()
    print(f"Diagnóstico de {len(tenders)} bases · OCR {'disponible' if use_ocr else 'NO disponible'} · no se guarda nada\n",
          flush=True)
    limit = config.RAG_MAX_BASES_MB * 1024 * 1024
    for code, title, url, bases_title, others in tenders:
        print(f"=== {code} · {(title or '')[:70]}", flush=True)
        if bases_title:
            print(f"   Documento leído: {bases_title}")
        if others:
            print(f"   Otros documentos publicados: {'; '.join(others)[:300]}")
        if not url:
            print("   No publica documento de bases.\n")
            continue
        try:
            content = b"".join(bases_download.open_bases(url, max_bytes=limit).chunks)
        except bases_download.BasesUnavailable as e:
            print(f"   No se pudo descargar: {e.reason}\n")
            continue
        for doc in bases_reader.explain(content, use_ocr=use_ocr):
            if "error" in doc:
                print(f"   {doc['document']}: {doc['error']}")
                continue
            scan = f" · escaneadas: {doc['image_pages']} (OCR en {doc['ocr_pages']})" if doc["image_pages"] else ""
            unread = " · ESCANEADO SIN LEER" if doc["unread_scan"] else ""
            print(f"   {doc['document']} · {doc['pages'] or 'Word'} págs.{scan}{unread} · "
                  f"líneas con «califica/habilitación»: {doc['total_lines']}")
            for page, line, verdict in doc["lines"]:
                where = f"pág. {page}" if page else "Word"
                print(f"     [{where}] {line}\n        → {verdict}")
        print(flush=True)
    return 0


def main_with(args):
    """Como main(), con los argumentos explícitos (lo usan las pruebas)."""
    return main(list(args))


def parse(args):
    parser = argparse.ArgumentParser(description="Ingesta diaria de convocatorias y lectura de bases (RAG).")
    parser.add_argument("--diagnostico", action="store_true")
    parser.add_argument("--sin-rag", action="store_true")
    parser.add_argument("--solo-rag", action="store_true")
    parser.add_argument("--buscar")
    parser.add_argument("--diagnosticar-bases", action="store_true")
    parser.add_argument("--releer-sin-seccion", action="store_true")
    parser.add_argument("--codigo", action="append")
    # Una opción mal escrita debe dar error: antes se ignoraba y se corría la ingesta completa
    return parser.parse_args(args)


def main(args=None):
    options = parse(sys.argv[1:] if args is None else args)
    if options.diagnostico:
        return run_diagnosis()
    if not config.DATABASE_URL:
        print("Falta DATABASE_URL (variable de entorno o app/.env). No se ejecutó la ingesta.")
        return 2
    engine = db.make_engine()
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    if options.diagnosticar_bases:
        code = run_bases_diagnosis(factory, options.codigo)
        engine.dispose()
        return code
    if options.buscar:
        code = run_search(factory, options.buscar, options.codigo)
        engine.dispose()
        return code
    if options.releer_sin_seccion:
        from sqlalchemy import update
        with factory.begin() as session:
            reset = session.execute(update(db.Tender).where(db.Tender.state == "vigente")
                                    .where(db.Tender.requirements_status == "seccion_no_encontrada")
                                    .values(requirements_status="pendiente")).rowcount
        print(f"{reset} bases sin la sección vuelven a quedar pendientes.", flush=True)
        options.solo_rag = True
    if options.solo_rag:
        lines = run_rag(factory)
        print("\n".join(lines))
        engine.dispose()
        return 0
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
        if not options.sin_rag:
            lines += run_rag(factory)
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