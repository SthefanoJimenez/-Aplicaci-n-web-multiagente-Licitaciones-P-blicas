"""HU-018: etapa de la convocatoria y cronograma leído de sus bases.

Usan la API simulada (tests/fake_ocds.py), SQLite y bases de prueba en PDF y Word: no necesitan
internet, Neon ni Gemini. El texto de remisión al SEACE es el de las bases reales revisadas el
13/11/2026 (1258549 y otras cinco).
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import inspect, text

import fake_ocds
import ingest_job
from app import app
from services import bases_download, bases_reader, db, ingest, ocds_source, rag, schedule, tenders
from test_en012 import FakeFile, fake_embed, filler, make_docx, make_pdf

LIMA = ocds_source.LIMA
NOW = datetime(2026, 11, 2, 8, 30, tzinfo=LIMA)
CODE = "2001"
SECTION_TEXT = """CAPÍTULO II
DEL PROCEDIMIENTO DE SELECCIÓN
2.1 CRONOGRAMA DEL PROCEDIMIENTO DE SELECCIÓN
Etapa | Fecha
Convocatoria | 02/11/2026
Formulación de consultas y observaciones | Del 03/11/2026 al 09/11/2026
Absolución de consultas y observaciones | 12/11/2026
Integración de las bases | 13 de noviembre de 2026
Presentación de ofertas
{ofertas}
Otorgamiento de la buena pro | 25/11/2026
2.2 CONTENIDO DE LAS OFERTAS
Los bienes se entregan conforme al siguiente cronograma de entregas: 30/12/2026"""
REAL_REFERRAL = """CAPÍTULO II
DEL PROCEDIMIENTO DE SELECCIÓN
2.1 CRONOGRAMA DEL PROCEDIMIENTO DE SELECCIÓN

      Según el cronograma de la ficha de selección de la convocatoria publicada en el SEACE de la
      Pladicop.

2.2 CONTENIDO DE LAS OFERTAS7"""
GENERAL_SECTION = ("La absolución de los referidos cuestionamientos y la publicación de las bases integradas se\n"
                   "realiza en la fecha prevista en el cronograma del\nprocedimiento de selección.")


def bases_pdf(ofertas="21/11/2026"):
    """Bases de 3 páginas: la sección general en la 1, el cronograma en la 2."""
    return make_pdf(["SECCIÓN GENERAL\n" + GENERAL_SECTION, SECTION_TEXT.format(ofertas=ofertas),
                     "CAPÍTULO III\nREQUERIMIENTO"])


# ---------------------------------------------------------------------------
# Etapa según los documentos publicados
# ---------------------------------------------------------------------------
def test_etapa_segun_los_documentos_publicados():
    administrativas = [{"title": "Bases Administrativas", "type": "biddingDocuments"},
                       {"title": "Resumen ejecutivo", "type": "tenderNotice"}]
    assert schedule.stage_of(administrativas) == "administrativas"
    assert schedule.stage_of(administrativas + [{"title": "Pliego de absolución de consultas y observaciones",
                                                 "type": "clarifications"}]) == "absolucion"
    # Basta el documentType de OCDS para aclaraciones, aunque el título no diga «pliego»
    assert schedule.stage_of(administrativas + [{"title": "Documento 3", "type": "clarifications"}]) == "absolucion"
    assert schedule.stage_of(administrativas + [{"title": "BASES INTEGRADAS", "type": "biddingDocuments"}]) == "integradas"
    assert schedule.stage_of([{"title": "Resumen ejecutivo", "type": "tenderNotice"}]) is None
    assert schedule.stage_of([]) is None


def test_etapa_con_los_documentos_reales_de_la_api():
    """Títulos y documentType de `python ingest_job.py --etapas 10` (13/11/2026)."""
    bases = {"title": "Bases Administrativas", "type": "biddingDocuments"}
    desierto = {"title": "Informe que sustenta la declaratoria de Desierto", "type": "evaluationReports"}
    acta = {"title": "Acta de no formulación de consultas y observaciones", "type": "clarifications"}
    integradas = {"title": "Bases Integradas", "type": "biddingDocuments"}
    assert schedule.stage_of([bases, desierto]) == "administrativas"     # el informe es del proceso anterior
    assert schedule.stage_of([bases, desierto, acta]) == "absolucion"
    assert schedule.stage_of([bases, acta, integradas]) == "integradas"
    # Contratación directa 1264631: ya publicó el otorgamiento de la buena pro
    assert schedule.stage_of([bases, {"title": "Documentos de Presentación de Propuestas", "type": "biddingDocuments"},
                              {"title": "Documentos de Otorgamiento de Buena Pro", "type": "awardNotice"}]) == "buena_pro"
    # También acepta las filas de la base (title, doc_type)
    row = db.TenderDocument(title="Bases Integradas", doc_type="biddingDocuments", url="x")
    assert schedule.stage_of([row]) == "integradas"


def test_pasos_de_la_etapa():
    assert schedule.stage_steps("absolucion") == [("Bases administrativas", "hecha"),
                                                  ("Absolución de consultas y observaciones", "actual"),
                                                  ("Bases integradas", "pendiente"),
                                                  ("Buena pro otorgada", "pendiente")]
    assert all(state == "pendiente" for _name, state in schedule.stage_steps(None))


# ---------------------------------------------------------------------------
# Cronograma en el texto de las bases
# ---------------------------------------------------------------------------
def test_lee_las_fechas_del_cronograma_con_su_pagina():
    result = bases_reader.read_schedule(bases_pdf())
    assert result.status == "leido" and result.page == 2 and result.document == "bases.pdf"
    events = {e["key"]: e for e in result.events}
    assert [e["key"] for e in result.events] == ["convocatoria", "consultas", "absolucion", "integracion",
                                                 "ofertas", "buena_pro"]
    assert events["consultas"]["start"] == "2026-11-03" and events["consultas"]["end"] == "2026-11-09"
    # «Absolución de consultas y observaciones» no se confunde con la formulación de consultas
    assert events["absolucion"]["start"] == "2026-11-12"
    assert events["integracion"]["start"] == "2026-11-13"                  # fecha escrita en letras
    assert events["ofertas"]["start"] == "2026-11-21"                      # fecha en la línea siguiente
    assert all(e["page"] == 2 for e in result.events)
    # El cronograma de entregas de la sección siguiente no es parte del cronograma del procedimiento
    assert "2026-12-30" not in {e["start"] for e in result.events}


def test_las_bases_reales_remiten_al_seace_y_no_se_inventan_fechas():
    result = bases_reader.read_schedule(make_pdf(["SECCIÓN GENERAL\n" + GENERAL_SECTION, REAL_REFERRAL]))
    assert result.status == "remite_seace" and result.page == 2 and result.events == []
    word = bases_reader.read_schedule(make_docx(REAL_REFERRAL))
    assert word.status == "remite_seace" and word.page is None and word.document == "bases.docx"


def test_sin_seccion_del_cronograma_o_sin_texto():
    # La sección general menciona «el cronograma del procedimiento de selección» dentro de una frase
    result = bases_reader.read_schedule(make_pdf([GENERAL_SECTION + "\n" + filler(4), "Plazo de entrega: 30/12/2026"]))
    assert result.status == "no_encontrado" and result.events == []
    assert bases_reader.read_schedule(make_pdf(["", ""])).status == "no_legible"
    empty = schedule.find_schedule("2.1 CRONOGRAMA DEL PROCEDIMIENTO DE SELECCIÓN\nVer anexo.\n2.2 CONTENIDO")
    assert empty.status == "no_encontrado" and empty.page is None


def test_fechas_validas_en_varios_formatos():
    assert schedule.dates_in("Del 03-11-2026 al 09/11/2026") == [date(2026, 11, 3), date(2026, 11, 9)]
    assert schedule.dates_in("el 5 de Setiembre del 2026") == [date(2026, 9, 5)]
    assert schedule.dates_in("31/02/2026 y 2026/11/03") == []            # fecha imposible y otro orden


def test_la_lectura_de_las_bases_para_el_rag_tambien_trae_el_cronograma():
    result = bases_reader.read_bases(bases_pdf(), use_ocr=False)
    assert result.schedule.status == "leido" and result.schedule.page == 2


# ---------------------------------------------------------------------------
# Ingesta: la etapa cambia y el cronograma se vuelve a leer
# ---------------------------------------------------------------------------
@pytest.fixture
def factory():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)
    yield factory
    tenders.use_session_factory(None)
    engine.dispose()


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(ocds_source, "REQUEST_PAUSE_SECONDS", 0)
    with fake_ocds.FakeOCDS() as fake:
        release = fake_ocds.release(int(CODE), NOW)
        # Como las Licitaciones Públicas Abreviadas reales: plazo de consultas en enquiryPeriod
        release["tender"]["enquiryPeriod"] = {"startDate": "2026-11-04T00:01:00-05:00",
                                              "endDate": "2026-11-06T23:59:00-05:00", "durationInDays": 2}
        fake.months[(2026, 11)] = [release]
        fake.months[(2026, 10)] = []
        yield fake


def run(factory, fake, now):
    session = ocds_source.session_for(fake.base + "/api/v1")
    return ingest.run_ingestion(factory, now=now, months=2, trigger="prueba",
                                fetch=lambda when, months: ocds_source.fetch_rows(when, months, session=session))


def add_document(fake, title, kind, url, published):
    release = fake.months[(2026, 11)][0]
    release["tender"]["documents"].append({"id": str(len(release["tender"]["documents"]) + 1), "title": title,
                                           "documentType": kind, "format": "pdf", "url": url,
                                           "datePublished": published.isoformat()})


FILES = {}


def fetch(url):
    if url not in FILES:
        raise bases_download.BasesUnavailable("el SEACE respondió 403")
    return FakeFile(FILES[url])


def sheet():
    response = app.test_client().get(f"/convocatorias/{CODE}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def tender_row(factory):
    with factory() as session:
        return session.query(db.Tender).filter_by(code=CODE).one()


def test_de_bases_administrativas_a_integradas(factory, source):
    administrativas = f"https://seace.example/{CODE}/bases.pdf"
    integradas = f"https://seace.example/{CODE}/bases-integradas.pdf"
    FILES.clear()
    FILES[administrativas] = bases_pdf("21/11/2026")
    FILES[integradas] = bases_pdf("28/11/2026")           # la entidad corrió la presentación de ofertas

    # 1. Convocatoria con bases administrativas: la etapa se conoce y el cronograma queda por leer
    assert run(factory, source, NOW)["status"] == "ok"
    row = tender_row(factory)
    assert row.stage == "administrativas" and row.schedule_due is True and row.schedule_status is None
    html = sheet()
    assert 'data-stage="administrativas"' in html and "El cronograma aún no se ha leído." in html
    assert "buscadorConvocatorias.xhtml" in html                         # siempre ofrece el SEACE

    summary = rag.read_due_schedules(factory, fetch=fetch)
    assert summary["leido"] == 1 and summary["remaining"] == 0
    row = tender_row(factory)
    assert row.schedule_status == "leido" and row.schedule_due is False and row.schedule_page == 2
    assert row.schedule_file == "Bases Administrativas · bases.pdf"
    html = sheet()
    assert "Cronograma actualizado al" in html and "21 nov 2026" in html and "3 nov 2026 al 9 nov 2026" in html
    assert "«Bases Administrativas · bases.pdf»" in html

    # 2. Se publica el pliego de absolución: cambia la etapa y el cronograma vuelve a quedar por leer
    add_document(source, "Pliego de absolución de consultas y observaciones", "clarifications",
                 f"https://seace.example/{CODE}/pliego.pdf", NOW + timedelta(days=10))
    later = NOW + timedelta(days=10)
    result = run(factory, source, later)
    assert result["stage_changes"] == 1
    row = tender_row(factory)
    assert row.stage == "absolucion" and row.schedule_due is True and row.schedule_status == "leido"
    html = sheet()
    assert 'aria-current="step">Absolución de consultas y observaciones' in html
    assert "El cronograma se volverá a leer en la próxima ingesta." in html
    assert "21 nov 2026" in html                                       # se conserva la lectura anterior
    assert 'Publicación de «Pliego de absolución de consultas y observaciones»</td><td style="white-space:nowrap;">12 nov 2026, 08:30' in html
    assert "Del 4 nov 2026, 00:01 al 6 nov 2026, 23:59" in html            # plazo de consultas de la API
    rag.read_due_schedules(factory, fetch=fetch)
    assert tender_row(factory).schedule_stage == "absolucion"

    # 3. Se publican las bases integradas: se leen de nuevo y la ficha muestra la fecha nueva
    add_document(source, "Bases Integradas", "biddingDocuments", integradas, NOW + timedelta(days=11))
    result = run(factory, source, NOW + timedelta(days=11))
    row = tender_row(factory)
    assert result["stage_changes"] == 1 and row.stage == "integradas" and row.schedule_due is True
    assert row.requirements_status == "pendiente"           # llegaron otras bases: el RAG también las relee
    rag.read_due_schedules(factory, fetch=fetch)
    row = tender_row(factory)
    assert row.schedule_file == "Bases Integradas · bases.pdf" and row.schedule_stage == "integradas"
    html = sheet()
    assert 'aria-current="step">Bases integradas' in html and "28 nov 2026" in html and "21 nov 2026" not in html
    assert "con las bases en la etapa «Bases integradas»" in html
    assert "se volverá a leer" not in html

    # 4. Una ingesta sin cambios no vuelve a marcar el cronograma
    assert run(factory, source, NOW + timedelta(days=12))["stage_changes"] == 0
    assert tender_row(factory).schedule_due is False


def test_bases_que_remiten_al_seace_y_descarga_fallida(factory, source):
    url = f"https://seace.example/{CODE}/bases.pdf"
    FILES.clear()
    run(factory, source, NOW)
    # La descarga falla: no se guarda nada y queda para la siguiente ejecución
    summary = rag.read_due_schedules(factory, fetch=fetch)
    assert summary["error"] == 1 and summary["remaining"] == 1
    assert tender_row(factory).schedule_status is None
    FILES[url] = make_pdf(["SECCIÓN GENERAL\n" + GENERAL_SECTION, REAL_REFERRAL])
    rag.read_due_schedules(factory, fetch=fetch)
    row = tender_row(factory)
    assert row.schedule_status == "remite_seace" and row.schedule is None
    html = sheet()
    assert "Las bases no traen las fechas:" in html and "página 2" in html
    assert 'data-schedule-status="remite_seace"' in html
    assert "Ver el cronograma en el buscador del SEACE" in html
    assert "Cronograma leído de las bases" not in html                  # ninguna fecha atribuida a las bases
    assert "Del 4 nov 2026, 00:01 al 6 nov 2026, 23:59" in html          # pero sí el plazo que publica la API


def test_una_lectura_peor_de_las_mismas_bases_no_borra_la_buena(factory, source):
    url = f"https://seace.example/{CODE}/bases.pdf"
    FILES.clear()
    FILES[url] = bases_pdf()
    run(factory, source, NOW)
    rag.read_due_schedules(factory, fetch=fetch)
    with factory.begin() as session:
        tender = session.query(db.Tender).filter_by(code=CODE).one()
        tender.schedule_due = True
    FILES[url] = make_pdf(["", ""])                                    # misma URL, ahora sin texto legible
    rag.read_due_schedules(factory, fetch=fetch)
    row = tender_row(factory)
    assert row.schedule_status == "leido" and len(row.schedule) == 6 and row.schedule_due is False


def test_el_rag_guarda_el_cronograma_de_las_bases_que_lee(factory, source):
    FILES.clear()
    FILES[f"https://seace.example/{CODE}/bases.pdf"] = bases_pdf()
    run(factory, source, NOW)
    rag.index_pending(factory, fetch=fetch, embed=fake_embed, use_ocr=False)
    row = tender_row(factory)
    assert row.requirements_status == "seccion_no_encontrada"     # estas bases de prueba no traen requisitos
    assert row.schedule_status == "leido" and row.schedule_due is False and len(row.schedule) == 6
    assert rag.read_due_schedules(factory, fetch=fetch)["processed"] == 0     # no se descargan dos veces


def test_sin_bases_no_hay_etapa(factory, source):
    source.months[(2026, 11)] = [fake_ocds.release(int(CODE), NOW, docs=False)]
    run(factory, source, NOW)
    row = tender_row(factory)
    assert row.stage is None and row.schedule_due is None
    assert "no se puede saber su etapa" in sheet()


def test_las_columnas_nuevas_se_agregan_a_una_base_existente(tmp_path):
    engine = db.make_engine(f"sqlite:///{tmp_path / 'vieja.sqlite3'}")
    db.create_tables(engine)
    added = {table: names for table, names in db.ADDED_COLUMNS.items() if table != "actualizaciones"}
    with engine.begin() as conn:                      # simula la base de Neon de antes de HU-018
        for table, names in added.items():
            for name in names:
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))
    db.create_tables(engine)
    inspector = inspect(engine)
    for table, names in added.items():
        assert set(names) <= {c["name"] for c in inspector.get_columns(table)}
    engine.dispose()


def test_comando_etapas_muestra_documentos_y_fechas_de_la_api(factory, source, monkeypatch, capsys):
    run(factory, source, NOW)
    compiled = {"tender": {"tenderPeriod": {"startDate": "2026-11-21T00:01:00-05:00", "endDate": "2026-11-21T00:01:00-05:00"},
                           "documents": [{"title": "Bases Administrativas", "documentType": "biddingDocuments",
                                          "datePublished": "2026-11-02T09:00:00-05:00"},
                                         {"title": "Pliego de absolución de consultas", "documentType": "clarifications",
                                          "datePublished": "2026-11-12T18:00:00-05:00"}]}}
    monkeypatch.setattr(ocds_source, "fetch_record", lambda ocid, session=None: (compiled, [compiled]))
    assert ingest_job.show_stages(factory, 10) == 0
    out = capsys.readouterr().out
    assert "«Pliego de absolución de consultas» · documentType: clarifications" in out
    assert "Etapa según la regla de HU-018: Absolución de consultas y observaciones" in out
    assert "¿Publica el pliego de absolución? sí" in out and "tenderPeriod en 1" in out

def test_la_app_agrega_las_columnas_nuevas_si_se_actualiza_antes_que_la_ingesta(tmp_path, monkeypatch):
    """13/11/2026: con los archivos nuevos y sin correr la ingesta, la lista decía «0 convocatorias vigentes»
    porque la consulta pedía columnas que Neon aún no tenía."""
    engine = db.make_engine(f"sqlite:///{tmp_path / 'neon.sqlite3'}")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    with factory.begin() as session:
        session.add(db.IngestionRun(status="ok", started_at=datetime(2026, 11, 13, 6, tzinfo=LIMA)))
        session.add(db.Tender(ocid="o-1", code=CODE, title="ADQUISICION DE DIESEL", state="vigente", rubros="1510",
                              published_at=datetime(2026, 11, 12, tzinfo=LIMA)))
    with engine.begin() as conn:                      # la base como estaba antes de esta versión
        for name in ("enquiry_start", "enquiry_end"):
            conn.execute(text(f"ALTER TABLE convocatorias DROP COLUMN {name}"))
    engine.dispose()                                  # SQLite: las conexiones guardadas recuerdan el esquema anterior
    monkeypatch.setattr(db, "make_engine", lambda *args, **kwargs: engine)
    tenders.use_session_factory(None)                 # la app abre la base como en producción
    try:
        html = app.test_client().get("/convocatorias").get_data(as_text=True)
        assert "ADQUISICION DE DIESEL" in html and "Resultados (1 convocatorias vigentes)" in html
        assert {"enquiry_start", "enquiry_end"} <= {c["name"] for c in inspect(engine).get_columns("convocatorias")}
    finally:
        tenders.use_session_factory(None)
        engine.dispose()


def test_si_la_consulta_falla_la_lista_avisa_en_lugar_de_mostrar_cero(factory, monkeypatch):
    with factory.begin() as session:
        session.add(db.IngestionRun(status="ok", started_at=datetime(2026, 11, 13, 6, tzinfo=LIMA)))

    def broken(**kwargs):
        raise tenders.TendersUnavailable("sin_conexion")

    monkeypatch.setattr(tenders, "open_tenders", broken)
    html = app.test_client().get("/convocatorias").get_data(as_text=True)
    assert "No pudimos consultar las convocatorias vigentes en este momento." in html