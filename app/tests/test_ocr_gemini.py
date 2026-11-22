"""Lectura con Gemini de las bases escaneadas (20/11/2026).

Las páginas escaneadas de las bases (documentos públicos) se envían a Gemini, que lee la imagen de la página;
si Gemini no responde, se usa tesseract como antes. No se llama a Gemini de verdad: se simula la respuesta.
"""
import io

import pytest
from pypdf import PdfWriter

import config
from services import bases_reader, llm
from test_en012 import REQUIREMENTS, filler


def scanned_pdf(pages):
    """PDF con páginas sin texto: para el lector, todas son escaneadas."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    calls = []

    def fake(content, page_numbers):
        calls.append(list(page_numbers))
        texts = {n: filler(3) for n in page_numbers}
        texts[page_numbers[-1]] = REQUIREMENTS
        return texts, 1234
    monkeypatch.setattr(bases_reader, "gemini_pages", fake)
    return calls


def test_separa_la_respuesta_por_pagina():
    answer = "=== PÁGINA 3 ===\nINDICE\n== Pagina 7 ==\n3.2 REQUISITOS DE CALIFICACIÓN\nA. CAPACIDAD LEGAL\n=== PÁGINA 99 ===\nx"
    assert bases_reader.split_marked_pages(answer, {3, 7}) == {3: "INDICE",
                                                              7: "3.2 REQUISITOS DE CALIFICACIÓN\nA. CAPACIDAD LEGAL"}


def test_envia_las_paginas_escaneadas_en_grupos_y_suma_los_tokens(monkeypatch):
    monkeypatch.setattr(bases_reader, "GEMINI_PAGES_PER_CALL", 2)
    seen = []

    def transcribe(pdf, prompt):
        groups = [int(n) for n in prompt.split("documento original: ")[1].split(".")[0].split(", ")]
        seen.append((len(bases_reader.pdf_pages(pdf)), groups))
        return "".join(f"=== PÁGINA {n} ===\ntexto {n}\n" for n in groups), 100
    texts, tokens = bases_reader.gemini_pages(scanned_pdf(6), [1, 3, 4, 6, 5], transcribe=transcribe)
    assert seen == [(2, [1, 3]), (2, [4, 6]), (1, [5])]
    assert texts == {1: "texto 1", 3: "texto 3", 4: "texto 4", 6: "texto 6", 5: "texto 5"} and tokens == 300


def test_una_base_escaneada_se_lee_con_gemini(gemini, monkeypatch):
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: False)      # sin tesseract en esta computadora
    result = bases_reader.read_bases(scanned_pdf(3))                         # use_ocr: sí, por Gemini
    assert gemini == [[1, 2, 3]]
    assert result.status == "extraido" and "Registro de Hidrocarburos" in result.section.text
    assert result.section.source == "ocr"


def test_si_gemini_no_responde_se_usa_tesseract(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")

    def broken(content, page_numbers):
        raise llm.ModelError("El modelo respondió con el error 429", code=429)
    monkeypatch.setattr(bases_reader, "gemini_pages", broken)
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: True)
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda content, pages: {n: REQUIREMENTS for n in pages})
    assert bases_reader.read_bases(scanned_pdf(2), use_ocr=True).status == "extraido"


def test_si_gemini_no_responde_y_no_hay_tesseract_queda_no_legible(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    monkeypatch.setattr(bases_reader, "gemini_pages",
                        lambda content, pages: (_ for _ in ()).throw(llm.ModelError("sin cuota", code=429)))
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: False)
    assert bases_reader.read_bases(scanned_pdf(2), use_ocr=True).status == "no_legible"


@pytest.mark.parametrize("engine, key, expected", [("auto", "clave", True), ("auto", None, False),
                                                   ("gemini", None, True), ("tesseract", "clave", False)])
def test_cuando_se_usa_gemini(monkeypatch, engine, key, expected):
    monkeypatch.setattr(config, "OCR_ENGINE", engine)
    monkeypatch.setattr(config, "GEMINI_API_KEY", key)
    assert bases_reader.gemini_ocr_enabled() is expected


def test_con_tesseract_no_se_llama_a_gemini(monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("no debía llamar a Gemini")
    monkeypatch.setattr(bases_reader, "gemini_pages", never)
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda content, pages: {n: REQUIREMENTS for n in pages})
    assert bases_reader.read_bases(scanned_pdf(2), use_ocr=True).status == "extraido"     # conftest: tesseract


def test_se_anota_con_que_se_leyeron_las_paginas_escaneadas(gemini, monkeypatch):
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: False)
    assert bases_reader.read_bases(scanned_pdf(2)).section.ocr_engine == "gemini"
    monkeypatch.setattr(config, "OCR_ENGINE", "tesseract")
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda content, pages: {n: REQUIREMENTS for n in pages})
    assert bases_reader.read_bases(scanned_pdf(2), use_ocr=True).section.ocr_engine == "tesseract"


def test_releer_escaneadas_lee_solo_las_de_tesseract_y_las_fallidas(tmp_path, monkeypatch, capsys):
    import ingest_job
    from services import db
    url = f"sqlite:///{tmp_path / 'base.db'}"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    read = []
    monkeypatch.setattr(ingest_job, "run_rag", lambda factory, codes=None: read.append(sorted(codes)) or ["(simulada)"])
    monkeypatch.setattr(ingest_job, "run_schedules", lambda factory, limit=None: [])
    engine = db.make_engine(url)
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    cases = {"t1": ("extraido", "ocr", None), "t2": ("extraido", "ocr", "tesseract"), "t3": ("extraido", "ocr", "gemini"),
             "t4": ("extraido", "texto", None), "t5": ("no_legible", None, None), "t6": ("seccion_no_encontrada", None, None),
             "t7": ("pendiente", None, None)}
    with factory.begin() as session:
        for code, (status, source, ocr_engine) in cases.items():
            tender = db.Tender(ocid=f"ocds-{code}", code=code, title="X", entity="E", state="vigente",
                               requirements_status=status)
            if source:
                tender.chunks.append(db.RequirementChunk(chunk_index=0, text="A", source=source,
                                                         ocr_engine=ocr_engine, embedding=[0.0] * 768))
            session.add(tender)
    monkeypatch.setattr(config, "OCR_ENGINE", "tesseract")
    assert ingest_job.main_with(["--releer-escaneadas"]) == 2               # sin Gemini no tiene sentido
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    assert ingest_job.main_with(["--releer-escaneadas"]) == 0
    assert "4 bases se vuelven a leer con Gemini" in capsys.readouterr().out
    assert read == [["t1", "t2", "t5", "t6"]]          # no la de Gemini, ni la de texto, ni la pendiente nueva
    with factory() as session:
        assert session.query(db.Tender).filter_by(code="t7").one().requirements_status == "pendiente"
    engine.dispose()


# --------------------------------------------------------------------------- 20/11/2026 (noche)
def test_si_con_gemini_no_aparece_la_seccion_se_prueba_con_tesseract(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    monkeypatch.setattr(bases_reader, "gemini_pages", lambda content, pages: ({n: filler(3) for n in pages}, 10))
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: True)
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda content, pages: {n: REQUIREMENTS for n in pages})
    result = bases_reader.read_bases(scanned_pdf(2), use_ocr=True)
    assert result.status == "extraido" and result.section.ocr_engine == "tesseract"
    assert result.used_gemini and not result.gemini_failed


def test_las_paginas_que_gemini_deja_vacias_se_leen_con_tesseract(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    monkeypatch.setattr(bases_reader, "gemini_pages", lambda content, pages: ({1: filler(3), 2: ""}, 10))
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: True)
    read = []
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda content, pages: read.append(list(pages)) or
                        {n: REQUIREMENTS for n in pages})
    result = bases_reader.read_bases(scanned_pdf(2), use_ocr=True)
    assert read[0] == [2] and result.status == "extraido" and result.section.ocr_engine == "gemini"


def test_se_anota_si_gemini_fallo(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    monkeypatch.setattr(bases_reader, "gemini_pages",
                        lambda content, pages: (_ for _ in ()).throw(llm.ModelError("sin cuota", code=429)))
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: True)
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda content, pages: {n: REQUIREMENTS for n in pages})
    result = bases_reader.read_bases(scanned_pdf(2), use_ocr=True)
    assert result.gemini_failed and not result.used_gemini


def test_releer_escaneadas_no_repite_las_ya_releidas_con_gemini(tmp_path, monkeypatch):
    import ingest_job
    from datetime import datetime, timezone
    from services import db, rag
    engine = db.make_engine(f"sqlite:///{tmp_path / 'b.db'}")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    with factory.begin() as session:
        for code, done in (("a", None), ("b", datetime.now(timezone.utc))):
            session.add(db.Tender(ocid=f"o-{code}", code=code, title="X", state="vigente",
                                  requirements_status="seccion_no_encontrada", gemini_read_at=done))
    assert ingest_job.scanned_codes(factory) == ["a"]
    # Al releer con Gemini disponible queda anotada; si Gemini falló, no
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    for failed, expected in ((True, None), (False, True)):
        with factory.begin() as session:
            tender = session.query(db.Tender).filter_by(code="a").one()
            if not tender.documents:
                tender.documents.append(db.TenderDocument(title="Bases", url="https://x", is_bases=True))
                session.flush()
            tender.gemini_read_at = None
            read = bases_reader.ReadResult("seccion_no_encontrada", detail="x", gemini_failed=failed)
            rag.index_tender(session, tender, prepared=(read, [], [], None))
            assert (tender.gemini_read_at is not None) == bool(expected)
    engine.dispose()


def test_ni_bien_gemini_se_queda_sin_cuota_se_pasa_a_tesseract(monkeypatch, capsys):
    """Pedido del PO (20/11/2026): «usar Gemini, pero ni bien se acaba, cambia a tesseract»."""
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    calls = []

    def out_of_quota(content, pages):
        calls.append(1)
        raise llm.ModelError("El modelo respondió con el error 429", code=429,
                             detail="Quota exceeded for metric: generate_content_requests_per_day")
    monkeypatch.setattr(bases_reader, "gemini_pages", out_of_quota)
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: True)
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda content, pages: {n: REQUIREMENTS for n in pages})
    first = bases_reader.read_bases(scanned_pdf(2), use_ocr=True)
    second = bases_reader.read_bases(scanned_pdf(2), use_ocr=True)
    assert calls == [1]                                  # la segunda ya no le pregunta a Gemini
    assert first.status == second.status == "extraido" and second.section.ocr_engine == "tesseract"
    assert first.gemini_failed and second.gemini_failed   # quedan para releer con Gemini después
    assert "se leen con tesseract desde ahora" in capsys.readouterr().out


def test_un_limite_por_minuto_no_apaga_gemini_hasta_tres_fallos_seguidos(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "gemini")
    calls = []

    def busy(content, pages):
        calls.append(1)
        raise llm.ModelError("El modelo respondió con el error 429", code=429, detail="per minute")
    monkeypatch.setattr(bases_reader, "gemini_pages", busy)
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: True)
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda content, pages: {n: REQUIREMENTS for n in pages})
    for _ in range(5):
        bases_reader.read_bases(scanned_pdf(1), use_ocr=True)
    assert len(calls) == bases_reader.GEMINI_FAILURES_TO_SWITCH and llm.exhausted("texto")


def test_que_errores_son_de_cuota_agotada():
    assert llm.out_of_quota(llm.ModelError("x", code=402))
    assert llm.out_of_quota(llm.ModelError("x", code=429, detail="GenerateRequestsPerDayPerProjectPerModel"))
    assert llm.out_of_quota(llm.ModelError("x", code=429, detail="Your project has exceeded its monthly spend cap"))
    assert not llm.out_of_quota(llm.ModelError("x", code=429, detail="GenerateRequestsPerMinutePerProject"))
    assert not llm.out_of_quota(llm.ModelError("x", code=503))