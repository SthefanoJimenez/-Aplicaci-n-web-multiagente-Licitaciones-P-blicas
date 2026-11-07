
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import app
from services import bases_reader, db, rag, requirements, tenders
from test_en012 import NOW, REQUIREMENTS, fake_embed, fake_fetch, seed


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
def indexed(factory):
    """Seis convocatorias con sus bases ya leídas por el RAG (EN-012)."""
    seed(factory)
    rag.index_pending(factory, limit=None, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
    return factory


@pytest.fixture
def client():
    return app.test_client()


def sheet(client, code):
    response = client.get(f"/convocatorias/{code}")
    return response.status_code, response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Armar los requisitos desde los fragmentos guardados
# ---------------------------------------------------------------------------
def test_overlap_finds_the_repeated_text_between_two_chunks():
    assert requirements.overlap("…del postor debe acreditar experiencia", "debe acreditar experiencia mínima") == 26
    assert requirements.overlap("texto anterior", "otro texto sin relación") == 0
    assert requirements.overlap("abc xyz", "xyz y más") == 0                 # menos de 20 caracteres: no cuenta


def test_merging_the_stored_chunks_gives_back_the_section_without_repeating_text():
    text = REQUIREMENTS * 4                                                 # ~3 000 caracteres: varios fragmentos
    middle = text.index("C. PERSONAL CLAVE", len(text) // 2)              # la página 13 empieza aquí
    section = bases_reader.Section(text=text, document="bases.pdf", source="texto",
                                   page_marks=[(0, 12), (middle, 13)])
    chunks = rag.chunk_section(section)
    assert len(chunks) >= 3
    blocks = requirements.merge_chunks([(page, piece) for piece, page in chunks])
    rebuilt = " ".join(" ".join(t for _k, t in b.segments) for b in blocks)
    assert " ".join(rebuilt.split()) == " ".join(text.split())             # nada perdido, nada repetido
    assert [b.page for b in blocks] == [12, 13]
    # cada parte en su página exacta: la 12 termina justo antes del cambio de página y la 13 empieza en él
    assert " ".join(blocks[0].text.split()) == " ".join(text[:middle].split())
    assert blocks[1].text.startswith("C. PERSONAL CLAVE")


def test_no_chunk_adds_text_from_two_pages():
    text = "\n".join(f"Línea {i} de los requisitos de calificación del postor." for i in range(120))
    marks = [(0, 30)] + [(text.index(f"Línea {i} "), 30 + i // 25) for i in range(25, 120, 25)]
    section = bases_reader.Section(text=text, document="bases.pdf", source="texto", page_marks=marks)
    previous_end = 0
    for piece, page in rag.chunk_section(section):
        new_text = piece[requirements.overlap(text[:previous_end].strip(), piece):] if previous_end else piece
        first_line = int(new_text.strip().split()[1])
        last_line = int(new_text.strip().split("\n")[-1].split()[1])
        assert 30 + first_line // 25 == page == 30 + last_line // 25        # el texto nuevo es de una sola página
        previous_end = text.index(new_text.strip().split("\n")[-1]) + len(new_text.strip().split("\n")[-1])


def test_headings_are_marked_and_page_footers_and_repeated_headers_removed():
    header = "MUNICIPALIDAD PROVINCIAL DE COTABAMBAS - SUBASTA INVERSA 077"
    blocks = requirements.merge_chunks([
        (33, f"{header}\nB. EXPERIENCIA DEL POSTOR\nEl postor debe acreditar S/ 300,000.00.\nPágina 9 de 11"),
        (34, f"{header}\nC. PERSONAL CLAVE\nUn ingeniero colegiado.\nPág. 10 / 11"),
    ])
    assert blocks[0].segments == [("titulo", "B. EXPERIENCIA DEL POSTOR"),
                                  ("texto", "El postor debe acreditar S/ 300,000.00.")]
    assert blocks[1].segments == [("titulo", "C. PERSONAL CLAVE"), ("texto", "Un ingeniero colegiado.")]


def test_page_headers_read_differently_by_ocr_are_still_removed():
    """Ejecución en Windows del 07/11/2026 (base 1263868): salía una "Página 40" con solo el encabezado."""
    header_a = "LICITACION PUBLICA ABREVIADA N.? 004-2026/INABIF - SEGUNDA CONVOCATORIA"
    header_b = "LICITACION PUBLICA ABREVIADA N.° 004-2026/INABIF - SEGUNDA CONVOCATORIA"
    blocks = requirements.merge_chunks([
        (38, "B. EXPERIENCIA DEL POSTOR\nEl postor acredita S/ 300,000.00."),
        (39, f"{header_a}\nC. PARTICIPACIÓN EN CONSORCIO\nC.1 El número máximo de consorciados es de 2"),
        (40, f"PROGRAMA INTEGRAL NACIONAL PARA EL BIENESTAR FAMILIAR - INABIF\n{header_b}"),
    ])
    assert [b.page for b in blocks] == [38, 39]
    assert blocks[1].segments[0] == ("titulo", "C. PARTICIPACIÓN EN CONSORCIO")


def test_a_last_page_with_real_requirements_is_kept():
    blocks = requirements.merge_chunks([(12, "A. CAPACIDAD LEGAL\nNo aplica."),
                                        (13, "B. EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD\nS/ 50,000.00")])
    assert [b.page for b in blocks] == [12, 13]


def test_word_titles_without_letters_and_loose_page_numbers():
    """Lo que se vio en las bases reales 1257079 (Word) y 1263973 (números de página sueltos)."""
    blocks = requirements.merge_chunks([(None, "REQUISITOS DE CALIFICACIÓN\nEXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD\n"
                                               "Requisitos:\nEl postor debe acreditar S/ 317,250.00.\n37\n"
                                               "REQUISITO DE CALIFICACIÓN ADICIONAL\nNo aplica")])
    assert blocks[0].segments == [("titulo", "REQUISITOS DE CALIFICACIÓN"),
                                  ("titulo", "EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD"),
                                  ("texto", "Requisitos:\nEl postor debe acreditar S/ 317,250.00."),
                                  ("titulo", "REQUISITO DE CALIFICACIÓN ADICIONAL"), ("texto", "No aplica")]


# ---------------------------------------------------------------------------
# Criterio 1: la ficha muestra los requisitos con el número de página
# ---------------------------------------------------------------------------
def test_sheet_shows_the_requirements_with_their_page(indexed, client):
    status, page = sheet(client, "1001")
    assert status == 200
    assert "Requisitos de calificación" in page and "ADQUISICION 1001" in page and "ENTIDAD 1001" in page
    assert "Página 2" in page and "página 2." in page                     # la sección está en la página 2 del PDF
    assert "B. EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD" in page
    assert "S/ 150,000.00" in page and "OSINERGMIN" in page
    assert "verifíquelos en las bases oficiales" in page
    assert "reconocimiento óptico" not in page                             # el PDF tenía texto: no es OCR


def test_word_bases_have_no_page_numbers_and_the_sheet_says_so(indexed, client):
    _status, page = sheet(client, "1002")
    assert "C. EQUIPAMIENTO ESTRATÉGICO" in page and "Superintendencia de Banca y Seguros" in page
    assert "Las bases vienen en Word, que no tiene números de página" in page
    assert 'class="chip">Página' not in page


def test_ocr_requirements_carry_a_warning(factory, client):
    seed(factory, codes=("1001",))
    with factory.begin() as session:
        tender = session.query(db.Tender).one()
        tender.requirements_status = "extraido"
        tender.chunks.append(db.RequirementChunk(chunk_index=0, text="A. CAPACIDAD LEGAL\nNo aplica.", page=33,
                                                 source="ocr", file_name="bases.pdf", embedding=fake_embed(["x"])[0],
                                                 model="prueba", tokens_est=5))
    _status, page = sheet(client, "1001")
    assert "Página 33" in page and "reconocimiento óptico (OCR)" in page and "bases.pdf" in page


def test_the_list_links_each_tender_to_its_sheet(indexed, client):
    page = client.get("/convocatorias").get_data(as_text=True)
    assert page.count("Ver requisitos") == 6 and 'href="/convocatorias/1001"' in page


# ---------------------------------------------------------------------------
# Criterio 2: si las bases no se pudieron leer, la ficha lo indica y ofrece descargarlas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code, status, message", [
    ("1003", "seccion_no_encontrada", "no encontramos la sección de requisitos de calificación"),
    ("1004", "error", "No pudimos descargar o leer las bases en la última actualización"),   # escaneado, sin OCR
    ("1005", "error", "No pudimos descargar o leer las bases en la última actualización"),   # el SEACE respondió 403
])
def test_unreadable_bases_are_explained_and_offered_for_download(indexed, client, code, status, message):
    _status, page = sheet(client, code)
    assert f'data-requirements-status="{status}"' in page and message in page
    assert f'href="/convocatorias/{code}/bases"' in page and "Descargar bases" in page
    assert "Enlace original del SEACE" in page and "Página " not in page.split("Requisitos de calificación")[1]


def test_bases_not_read_yet_say_they_will_be_read_tomorrow(factory, client):
    seed(factory, codes=("1001",))                                          # sin pasar por el RAG
    _status, page = sheet(client, "1001")
    assert 'data-requirements-status="pendiente"' in page and "aún no se han leído" in page
    assert 'href="/convocatorias/1001/bases"' in page


def test_tender_without_bases_points_to_the_seace_search(indexed, client):
    _status, page = sheet(client, "1006")
    assert "no publica bases en el portal del OECE" in page and "buscador del SEACE" in page
    assert "/convocatorias/1006/bases" not in page


def test_marked_as_read_but_without_fragments_is_treated_as_unreadable(factory, client):
    seed(factory, codes=("1001",))
    with factory.begin() as session:
        session.query(db.Tender).one().requirements_status = "extraido"
    _status, page = sheet(client, "1001")
    assert 'data-requirements-status="no_legible"' in page


# ---------------------------------------------------------------------------
# Otros casos de la ficha
# ---------------------------------------------------------------------------
def test_closed_tender_still_shows_its_sheet_with_a_notice(indexed, client):
    with indexed.begin() as session:
        tender = session.query(db.Tender).filter_by(code="1001").one()
        tender.state, tender.closed_at = "no_vigente", NOW - timedelta(days=1)
    _status, page = sheet(client, "1001")
    assert "Esta convocatoria ya no está vigente" in page and "B. EXPERIENCIA DEL POSTOR" in page


def test_unknown_code_is_404_and_a_database_failure_is_503(indexed, client, monkeypatch):
    assert client.get("/convocatorias/999999").status_code == 404

    def down(code):
        raise tenders.TendersUnavailable("sin_conexion")
    monkeypatch.setattr(tenders, "tender_sheet", down)
    status, page = sheet(client, "1001")
    assert status == 503 and "la base de datos no respondió" in page



def test_requirements_command_suggests_codes_and_prints_them_like_the_sheet(tmp_path, monkeypatch, capsys):
    import config
    import ingest_job
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'base.db'}")
    engine = db.make_engine(config.DATABASE_URL)
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    seed(factory)
    rag.index_pending(factory, limit=None, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
    engine.dispose()
    assert ingest_job.main_with(["--requisitos"]) == 0
    out = capsys.readouterr().out
    assert "- Requisitos de un PDF con texto: 1001" in out and "- Requisitos de un Word (sin páginas): 1002" in out
    assert "- Requisitos leídos con OCR: ninguna todavía" in out
    assert "- Bases seccion_no_encontrada: 1003 (1 vigente en ese estado)" in out
    assert ingest_job.main_with(["--requisitos", "1001", "1002", "1003", "777"]) == 0
    out = capsys.readouterr().out
    assert "extraido · " in out and "página 2 · texto" in out and "Word, sin páginas" in out
    assert "B. EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD" in out
    assert "seccion_no_encontrada: Leímos las bases" in out and "777: no existe en la base." in out


def test_reread_option_reads_only_the_given_codes_again(tmp_path, monkeypatch, capsys):
    import config
    import ingest_job
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'base.db'}")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "clave-de-prueba")
    engine = db.make_engine(config.DATABASE_URL)
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    seed(factory, codes=("1001", "1002", "1003"))
    monkeypatch.setattr(rag.bases_download, "open_bases", lambda url, max_bytes=None: fake_fetch(url))
    monkeypatch.setattr(rag.llm, "embed_texts", fake_embed)
    assert ingest_job.main_with(["--releer", "1001"]) == 0
    out = capsys.readouterr().out
    assert "Volviendo a leer las bases de 1 convocatoria(s)" in out and "1/1 · 1001 · extraido" in out
    with factory() as session:
        states = {t.code: t.requirements_status for t in session.query(db.Tender)}
    engine.dispose()
    assert states == {"1001": "extraido", "1002": "pendiente", "1003": "pendiente"}   # las demás no se tocan


def test_unreadable_status_has_its_own_message(indexed, client):
    with indexed.begin() as session:
        session.query(db.Tender).filter_by(code="1003").one().requirements_status = "no_legible"
    _status, page = sheet(client, "1003")
    assert 'data-requirements-status="no_legible"' in page and "No pudimos leer las bases de esta convocatoria" in page


# ---------------------------------------------------------------------------
# Volver a leer en una computadora sin OCR no borra lo que ya se había leído (07/11/2026, base 1263868)
# ---------------------------------------------------------------------------
def test_a_worse_reread_keeps_the_requirements_already_read(indexed, client):
    with indexed() as session:
        before = [c.text for c in session.query(db.RequirementChunk).join(db.Tender).filter(db.Tender.code == "1001")]
    scanned = {"https://prod1.seace.gob.pe/1001": "https://prod1.seace.gob.pe/1004"}   # ahora "llega" un escaneado
    summary = rag.reindex_codes(indexed, ["1001"], fetch=lambda url: fake_fetch(scanned.get(url, url)),
                                embed=fake_embed, use_ocr=False)
    assert summary["conservado"] == 1 and summary["details"][0][1] == "conservado"
    assert "se conserva la lectura anterior" in summary["details"][0][2]
    with indexed() as session:
        tender = session.query(db.Tender).filter_by(code="1001").one()
        after = [c.text for c in session.query(db.RequirementChunk).filter_by(tender_id=tender.id)]
        assert tender.requirements_status == "extraido" and after == before
    _status, page = sheet(client, "1001")
    assert "B. EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD" in page


def test_scanned_bases_without_ocr_stay_pending_for_a_computer_with_ocr(factory):
    seed(factory, codes=("1004",))
    summary = rag.index_pending(factory, limit=None, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
    assert summary["details"][0][1] == "error" and "se reintentará en una computadora con OCR" in summary["details"][0][2]
    with factory() as session:
        assert rag.pending_ids(session, None)                                   # sigue en la cola



# ---------------------------------------------------------------------------
# OCR en Windows: la app encuentra tesseract y pdftoppm aunque no estén en el PATH
# ---------------------------------------------------------------------------
@pytest.fixture
def no_path(monkeypatch):
    monkeypatch.setattr(bases_reader.shutil, "which", lambda name: None)
    # Sin esto, en una PC con Windows que sí tiene OCR se encontraría la instalación real
    monkeypatch.setattr(bases_reader, "WINDOWS_TESSERACT", [])
    monkeypatch.setattr(bases_reader, "WINDOWS_POPPLER", [])
    monkeypatch.delenv("TESSERACT_PATH", raising=False)
    monkeypatch.delenv("POPPLER_PATH", raising=False)
    bases_reader.tool_path.cache_clear()
    yield monkeypatch
    bases_reader.tool_path.cache_clear()


def test_tool_paths_from_the_env_file_accept_the_exe_or_its_folder(no_path, tmp_path):
    exe = tmp_path / "Tesseract-OCR" / "tesseract.exe"
    exe.parent.mkdir()
    exe.write_text("")
    poppler = tmp_path / "poppler-26.09.0" / "Library" / "bin"
    poppler.mkdir(parents=True)
    (poppler / "pdftoppm.exe").write_text("")
    no_path.setenv("TESSERACT_PATH", f'"{exe}"')                     # con comillas, como se suele copiar
    no_path.setenv("POPPLER_PATH", str(poppler))                      # la carpeta
    assert bases_reader.tool_path("tesseract") == str(exe)
    assert bases_reader.tool_path("pdftoppm") == str(poppler / "pdftoppm.exe")


def test_on_windows_the_usual_install_folders_are_searched(no_path, tmp_path):
    for version in ("poppler-24.08.0", "poppler-26.09.0"):
        folder = tmp_path / version / "Library" / "bin"
        folder.mkdir(parents=True)
        (folder / "pdftoppm.exe").write_text("")
    no_path.setattr(bases_reader.os, "name", "nt")
    no_path.setattr(bases_reader, "WINDOWS_POPPLER", [str(tmp_path / "*" / "Library" / "bin" / "pdftoppm.exe")])
    no_path.setattr(bases_reader, "WINDOWS_TESSERACT", [str(tmp_path / "no-instalado" / "tesseract.exe")])
    assert bases_reader.tool_path("pdftoppm").endswith("poppler-26.09.0/Library/bin/pdftoppm.exe".replace("/", bases_reader.os.sep))
    assert bases_reader.tool_path("tesseract") is None
    assert bases_reader.ocr_available() is False


def test_ocr_command_says_where_the_tools_are(no_path, capsys):
    import ingest_job
    assert ingest_job.main_with(["--ocr"]) == 1
    out = capsys.readouterr().out
    assert "tesseract: no encontrado" in out and "pdftoppm:  no encontrado" in out
    assert "TESSERACT_PATH / POPPLER_PATH" in out