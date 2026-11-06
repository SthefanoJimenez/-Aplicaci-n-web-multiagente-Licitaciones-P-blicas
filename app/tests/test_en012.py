"""EN-012: RAG de las bases (lectura de la sección de requisitos, fragmentos, vectores y búsqueda).

No usan internet ni la API de Gemini: los PDF y Word se arman en la prueba, la descarga se
simula y los vectores salen de una bolsa de palabras (palabras en común = vectores parecidos).
La prueba con PostgreSQL y pgvector corre solo si RAG_TEST_POSTGRES_URL apunta a una base de prueba.
"""
import hashlib
import io
import os
import re
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import config
from services import bases_download, bases_reader, db, ingest, llm, rag

NOW = datetime.now(timezone.utc)

REQUIREMENTS = """CAPÍTULO III
3.2 REQUISITOS DE CALIFICACIÓN
A. CAPACIDAD LEGAL - HABILITACIÓN
Requisito: Copia de la constancia de inscripción vigente en el Registro de Hidrocarburos de OSINERGMIN.
B. EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD
Requisito: El postor debe acreditar un monto facturado acumulado equivalente a S/ 150,000.00 por la venta
de bienes iguales o similares al objeto de la convocatoria, durante los ocho años anteriores.
Se consideran bienes similares: gasolina, diésel y gas licuado de petróleo.
Acreditación: copia simple de contratos u órdenes de compra y su respectiva conformidad.
C. PERSONAL CLAVE
Requisito: Un ingeniero químico o de petróleo colegiado y habilitado, con experiencia mínima de dos años.
"""
OTHER = """3.4 REQUISITOS DE CALIFICACIÓN
A. GARANTÍA DE FIEL CUMPLIMIENTO
El postor ganador entrega una carta fianza por el diez por ciento del monto del contrato original.
Acreditación: la carta fianza debe ser incondicional, solidaria, irrevocable y de realización automática
en el país, emitida por una empresa bajo la supervisión de la Superintendencia de Banca y Seguros.
B. HABILITACIÓN
Requisito: Licencia municipal de funcionamiento del local de venta de vehículos y repuestos, y
autorización del fabricante o representante de la marca para la comercialización de los vehículos.
C. EQUIPAMIENTO ESTRATÉGICO
Requisito: Taller de servicio autorizado en la región, con elevador, equipo de diagnóstico electrónico y
almacén de repuestos originales. Acreditación: copia de documentos que sustenten la propiedad o el alquiler.
"""
PROCEDURE = ("La evaluación de las ofertas y la revisión de los requisitos de calificación se realiza "
             "posteriormente a la evaluación de las ofertas, según el procedimiento de la Ley.\n")


# ---------------------------------------------------------------------------
# Archivos de prueba
# ---------------------------------------------------------------------------
def make_pdf(pages):
    """PDF mínimo con texto (Helvetica, WinAnsi) que pypdf puede leer; una cadena por página."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for text in pages:
        lines = []
        for line in text.split("\n"):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"({escaped}) Tj T*")
        stream = ("BT /F1 10 Tf 12 TL 40 800 Td " + " ".join(lines) + " ET").encode("cp1252")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_ref = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                       f"/Contents {content_ref} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        body = obj if isinstance(obj, bytes) else obj.encode("cp1252")
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def make_docx(text):
    paragraphs = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{line}</w:t></w:r></w:p>" for line in text.split("\n"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                         f'wordprocessingml/2006/main"><w:body>{paragraphs}</w:body></w:document>')
        archive.writestr("[Content_Types].xml", "<Types/>")
    return buffer.getvalue()


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def filler(n):
    return "\n".join(f"Línea {i} del documento con información general del procedimiento de selección."
                     for i in range(n))


# ---------------------------------------------------------------------------
# Vectores simulados
# ---------------------------------------------------------------------------
def _words(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return [w for w in re.findall(r"[a-z]{4,}", text)]


def fake_embed(texts, task_type="RETRIEVAL_DOCUMENT"):
    vectors = []
    for text in texts:
        v = [0.0] * config.EMBEDDING_DIM
        for w in _words(text):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % config.EMBEDDING_DIM] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        vectors.append([x / norm for x in v])
    return vectors


class FakeFile:
    def __init__(self, data):
        self.chunks = iter([data[i:i + 5000] for i in range(0, len(data), 5000)])


# ---------------------------------------------------------------------------
# Lectura de las bases
# ---------------------------------------------------------------------------
def test_finds_the_heading_section_and_skips_mentions_and_index_lines():
    index = "ÍNDICE\n3.2 REQUISITOS DE CALIFICACIÓN .................................. 12\n"
    result = bases_reader.read_bases(make_docx(index + PROCEDURE + filler(5) + "\n" + REQUIREMENTS), use_ocr=False)
    assert result.status == "extraido"
    assert result.section.text.startswith("REQUISITOS DE CALIFICACIÓN\nA. CAPACIDAD LEGAL")
    assert "monto facturado acumulado" in result.section.text and "...." not in result.section.text
    assert result.section.page_at(0) is None                       # Word no tiene páginas


def test_pdf_section_keeps_the_page_of_each_part():
    first, second = REQUIREMENTS.split("C. PERSONAL CLAVE")
    pdf = make_pdf([filler(10), PROCEDURE + filler(5), filler(3) + "\n" + first, "C. PERSONAL CLAVE" + second])
    result = bases_reader.read_bases(pdf, use_ocr=False)
    assert result.status == "extraido" and result.section.source == "texto"
    section = result.section
    assert section.page_at(0) == 3
    assert section.page_at(section.text.index("PERSONAL CLAVE")) == 4


def test_scanned_without_ocr_unknown_files_and_missing_section():
    scanned = bases_reader.read_bases(make_pdf(["", "", ""]), use_ocr=False)
    assert scanned.status == "no_legible" and "OCR" in scanned.detail
    assert bases_reader.read_bases(b"<html>Sesion expirada</html>", use_ocr=False).status == "no_legible"
    no_section = bases_reader.read_bases(make_pdf([filler(20), PROCEDURE]), use_ocr=False)
    assert no_section.status == "seccion_no_encontrada"


def test_opens_pdfs_inside_a_zip():
    archive = make_zip({"anexos.pdf": make_pdf([filler(5)]), "bases integradas.pdf": make_pdf([filler(5), REQUIREMENTS])})
    result = bases_reader.read_bases(archive, use_ocr=False)
    assert result.status == "extraido" and result.section.document == "bases integradas.pdf"


def test_chunks_overlap_and_carry_their_page():
    section = bases_reader.Section(text=REQUIREMENTS * 4, document="b.pdf", source="texto",
                                   page_marks=[(0, 7), (len(REQUIREMENTS) * 2, 8)])
    pieces = rag.chunk_section(section)
    assert len(pieces) >= 3
    assert all(len(p) <= rag.CHUNK_CHARS for p, _ in pieces)
    assert pieces[0][1] == 7 and pieces[-1][1] == 8
    assert pieces[0][0][-60:].strip()[:30] in section.text           # el texto sale tal cual de la sección
    joined = "".join(p for p, _ in pieces)
    assert all(word in joined for word in ("OSINERGMIN", "150,000.00", "ingeniero químico"))


# ---------------------------------------------------------------------------
# Indexación y búsqueda
# ---------------------------------------------------------------------------
@pytest.fixture
def factory():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    yield db.make_session_factory(engine)
    engine.dispose()


FILES = {
    "https://prod1.seace.gob.pe/1001": make_pdf([filler(8), PROCEDURE + REQUIREMENTS]),
    "https://prod1.seace.gob.pe/1002": make_docx(filler(3) + "\n" + OTHER),
    "https://prod1.seace.gob.pe/1003": make_pdf([filler(30), PROCEDURE]),
    "https://prod1.seace.gob.pe/1004": make_pdf(["", ""]),
}


def fake_fetch(url):
    if url not in FILES:
        raise bases_download.BasesUnavailable("el SEACE respondió 403")
    return FakeFile(FILES[url])


def seed(factory, codes=("1001", "1002", "1003", "1004", "1005", "1006")):
    with factory.begin() as session:
        for i, code in enumerate(codes):
            tender = db.Tender(ocid=f"ocds-dgv273-seacev3-{code}", code=code, title=f"ADQUISICION {code}",
                               entity=f"ENTIDAD {code}", state="vigente", published_at=NOW - timedelta(days=i))
            if code != "1006":                               # 1006 no publica bases
                tender.documents.append(db.TenderDocument(title="Bases Administrativas", url=f"https://prod1.seace.gob.pe/{code}",
                                                          doc_format="pdf", is_bases=True))
            session.add(tender)


def statuses(factory):
    with factory() as session:
        return {t.code: t.requirements_status for t in session.query(db.Tender)}


def test_index_pending_reads_each_bases_and_records_why_it_could_not(factory):
    seed(factory)
    summary = rag.index_pending(factory, limit=10, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
    assert statuses(factory) == {"1001": "extraido", "1002": "extraido", "1003": "seccion_no_encontrada",
                                 "1004": "no_legible", "1005": "error", "1006": "no_legible"}
    assert summary["processed"] == 6 and summary["extraido"] == 2 and summary["error"] == 1
    assert summary["remaining"] == 1                         # 1005 se reintenta en la próxima ejecución
    with factory() as session:
        chunks = session.query(db.RequirementChunk).join(db.Tender).filter(db.Tender.code == "1001").all()
        assert chunks and all(c.page == 2 and c.source == "texto" and len(c.embedding) == config.EMBEDDING_DIM
                              for c in chunks)
        assert summary["tokens"] == sum(c.tokens_est for c in session.query(db.RequirementChunk))
    # La segunda ejecución solo reintenta la que falló
    calls = []
    rag.index_pending(factory, limit=10, fetch=lambda url: calls.append(url) or fake_fetch(url), embed=fake_embed, use_ocr=False)
    assert calls == ["https://prod1.seace.gob.pe/1005"]


def test_search_returns_the_right_tender_and_page(factory):
    seed(factory, codes=("1001", "1002"))
    rag.index_pending(factory, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
    with factory() as session:
        top = rag.search(session, "monto facturado de experiencia en venta de diésel", k=3, embed=fake_embed)
        assert top[0]["code"] == "1001" and top[0]["page"] == 2 and "facturado" in top[0]["text"]
        assert top[0]["bases_url"] == "https://prod1.seace.gob.pe/1001"
        guarantee = rag.search(session, "carta fianza de garantía de fiel cumplimiento", k=1, embed=fake_embed)
        assert guarantee[0]["code"] == "1002" and guarantee[0]["page"] is None
        only = rag.search(session, "carta fianza", k=5, codes=["1001"], embed=fake_embed)
        assert {r["code"] for r in only} == {"1001"}


def test_gemini_failure_stops_the_run_and_keeps_the_tender_pending(factory):
    seed(factory, codes=("1001", "1002"))

    def broken(texts, task_type):
        raise llm.ModelError("El modelo respondió con el error 429", code=429)

    summary = rag.index_pending(factory, fetch=fake_fetch, embed=broken, use_ocr=False)
    assert summary["stopped"] and summary["processed"] == 0
    assert statuses(factory) == {"1001": "pendiente", "1002": "pendiente"}
    with factory() as session:
        assert session.query(db.RequirementChunk).count() == 0


def test_new_bases_replace_the_old_fragments(factory):
    seed(factory, codes=("1001",))
    rag.index_pending(factory, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
    with factory.begin() as session:
        tender = session.query(db.Tender).one()
        before = len(tender.chunks)
        # La ingesta trae las bases integradas: el RAG debe volver a leerlas
        row = {"documents": [{"title": "Bases Integradas", "type": "biddingDocuments", "format": "docx",
                              "url": "https://prod1.seace.gob.pe/1002"}]}
        ingest._sync_documents(tender, row)
        assert tender.requirements_status == "pendiente"
    rag.index_pending(factory, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
    with factory() as session:
        tender = session.query(db.Tender).one()
        assert tender.requirements_status == "extraido" and before > 0
        assert all("fianza" in c.text or "HABILITACIÓN" in c.text or "Licencia" in c.text for c in tender.chunks)


def test_job_indexes_after_ingestion_and_search_option(tmp_path, monkeypatch, capsys, factory):
    import ingest_job
    url = f"sqlite:///{tmp_path / 'base.db'}"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "clave-de-prueba")
    monkeypatch.setattr(llm, "embed_texts", fake_embed)
    monkeypatch.setattr(bases_download, "open_bases", lambda url, **kwargs: fake_fetch(url))
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: False)
    engine = db.make_engine(url)
    db.create_tables(engine)
    seed(db.make_session_factory(engine), codes=("1001",))
    engine.dispose()
    assert ingest_job.main_with(["--solo-rag"]) == 0
    assert "con requisitos: 1" in capsys.readouterr().out
    assert ingest_job.main_with(["--buscar", "experiencia en venta de diésel", "--codigo", "1001"]) == 0
    out = capsys.readouterr().out
    assert "1001 · ENTIDAD 1001 · pág. 2" in out and "clave-de-prueba" not in out


def test_without_gemini_key_the_rag_step_is_skipped(monkeypatch, factory):
    import ingest_job
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    assert "omitida" in ingest_job.run_rag(factory)[0]


def test_progress_is_reported_for_each_bases(factory):
    seed(factory, codes=("1001", "1003"))
    seen = []
    rag.index_pending(factory, fetch=fake_fetch, embed=fake_embed, use_ocr=False,
                      progress=lambda n, total, code, status, secs: seen.append((n, total, code, status)))
    assert seen == [(1, 2, "1001", "extraido"), (2, 2, "1003", "seccion_no_encontrada")]


class SizedResponse:
    """Respuesta del SEACE con un PDF de `size` bytes, con o sin Content-Length."""

    def __init__(self, size, declare=True):
        self.status_code, self.url, self.size = 200, "https://prod1.seace.gob.pe/archivo", size
        self.headers = {"Content-Length": str(size)} if declare else {}

    def iter_content(self, chunk):
        body = b"%PDF-1.7\n" + b"x" * (self.size - 9)
        for i in range(0, len(body), chunk):
            yield body[i:i + chunk]

    def close(self):
        pass


class SizedSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        return self.response


def test_size_limit_is_larger_for_reading_and_never_truncates_silently():
    url = "https://prod1.seace.gob.pe/archivo"
    big = 3 * 1024 * 1024
    with pytest.raises(bases_download.BasesUnavailable) as error:
        bases_download.open_bases(url, session=SizedSession(SizedResponse(big)), max_bytes=1024 * 1024)
    assert "3 MB" in error.value.reason
    ok = bases_download.open_bases(url, session=SizedSession(SizedResponse(big)), max_bytes=5 * 1024 * 1024)
    assert len(b"".join(ok.chunks)) == big
    # Sin Content-Length: antes se cortaba en silencio (archivo dañado); ahora la descarga falla
    undeclared = bases_download.open_bases(url, session=SizedSession(SizedResponse(big, declare=False)),
                                           max_bytes=1024 * 1024)
    with pytest.raises(bases_download.BasesUnavailable):
        b"".join(undeclared.chunks)
    assert config.RAG_MAX_BASES_MB * 1024 * 1024 > bases_download.MAX_BYTES


def test_heading_with_propuesta_de_prefix_is_recognized():
    # Así titula el requerimiento la base 1248982 del SP-005 (página 32)
    text = (filler(4) + "\nPROPUESTA DE REQUISITOS DE CALIFICACIÓN\n10.1. PROPUESTA DE REQUISITOS DE CALIFICACIÓN "
            "OBLIGATORIOS\n" + REQUIREMENTS.split("3.2 REQUISITOS DE CALIFICACIÓN\n")[1])
    result = bases_reader.read_bases(make_docx(text), use_ocr=False)
    assert result.status == "extraido" and "monto facturado acumulado" in result.section.text


def test_ocr_numbering_with_comma_is_recognized():
    # El OCR de la base 1262730 leyó "10." como "10," y "10.1" como "10,1"
    body = REQUIREMENTS.split("3.2 REQUISITOS DE CALIFICACIÓN\n")[1]
    pdf = make_pdf([filler(10), PROCEDURE, "10, REQUISITOS DE CALIFICACIÓN\n10,1 REQUISITOS DE CALIFICACIÓN OBLIGATORIOS\n" + body])
    result = bases_reader.read_bases(pdf, use_ocr=False)
    assert result.status == "extraido" and result.section.page_at(0) == 3
    assert "monto facturado acumulado" in result.section.text


def pdf_with_image_page(pages, image_page):
    """PDF de prueba donde la página `image_page` (desde 1) trae además una imagen de 600 × 800 px."""
    import zlib
    pdf = make_pdf(pages)
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    image = DecodedStreamObject()
    image.set_data(bytes(600 * 800))
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(600), NameObject("/Height"): NumberObject(800),
                  NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): NumberObject(8)})
    page = writer.pages[image_page - 1]
    resources = page["/Resources"]
    resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im1"): writer._add_object(image)})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_page_with_big_image_and_only_a_header_is_read_with_ocr(monkeypatch):
    # Base 1258549: el Capítulo III son fotos con 212 caracteres de encabezado (sobre el límite de 200)
    header = "LICITACIÓN PÚBLICA ABREVIADA PARA BIENES\nEMPRESA MUNICIPAL DE AGUA POTABLE Y ALCANTARILLADO\n" * 2
    pdf = pdf_with_image_page([filler(30), header], image_page=2)
    asked = []
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda data, numbers: asked.extend(numbers) or {n: REQUIREMENTS for n in numbers})
    result = bases_reader.read_bases(pdf, use_ocr=True)
    assert asked == [2]
    assert result.status == "extraido" and result.section.source == "ocr" and result.section.page_at(0) == 2


def make_docx_with_images(text, images):
    buffer = io.BytesIO()
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n"))
    body += "".join(f'<w:p><w:r><w:drawing><a:blip r:embed="rId{i}"/></w:drawing></w:r></w:p>' for i in range(len(images)))
    rels = "".join(f'<Relationship Id="rId{i}" Target="media/image{i}.jpg"/>' for i in range(len(images)))
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="w" xmlns:a="a" xmlns:r="r"><w:body>'
                         f"{body}</w:body></w:document>")
        archive.writestr("word/_rels/document.xml.rels", f"<Relationships>{rels}</Relationships>")
        for i, data in enumerate(images):
            archive.writestr(f"word/media/image{i}.jpg", data)
    return buffer.getvalue()


def test_word_images_are_read_only_when_the_text_has_no_section(monkeypatch):
    # Base 1255879: el Capítulo III está pegado como fotos dentro del Word
    images = [b"logo" * 100, b"A" * 40_000, b"B" * 40_000]          # el logo pesa poco y se omite
    read = []

    def fake_ocr(name, data):
        read.append(name)
        return "CAPÍTULO III\n" + REQUIREMENTS if data[:1] == b"B" else "Especificaciones técnicas del bien."

    monkeypatch.setattr(bases_reader, "ocr_image", fake_ocr)
    docx = make_docx_with_images(filler(5) + "\n" + PROCEDURE, images)
    result = bases_reader.read_bases(docx, use_ocr=True)
    assert read == ["image1.jpg", "image2.jpg"]
    assert result.status == "extraido" and result.section.source == "ocr"
    assert "monto facturado acumulado" in result.section.text
    read.clear()
    assert bases_reader.read_bases(make_docx_with_images(REQUIREMENTS, images), use_ocr=True).section.source == "texto"
    assert read == []                                                # con la sección en el texto, no hay OCR


def test_text_section_wins_over_the_same_section_in_a_photo(monkeypatch):
    # 1250704 y 1257132: el requerimiento repite la sección como foto (más larga, con firmas y sellos)
    header = "ENCABEZADO DE LA ENTIDAD CONVOCANTE Y NÚMERO DEL PROCEDIMIENTO DE SELECCIÓN\n" * 3
    pdf = pdf_with_image_page([filler(10), header, filler(5), REQUIREMENTS], image_page=2)
    monkeypatch.setattr(bases_reader, "ocr_pages", lambda data, numbers: {n: REQUIREMENTS * 3 for n in numbers})
    result = bases_reader.read_bases(pdf, use_ocr=True)
    assert result.section.source == "texto" and result.section.page_at(0) == 4


def test_word_photos_are_read_only_if_no_document_has_the_section(monkeypatch):
    # 1248982: el Word del RAR trae fotos de otra cosa; el PDF sí trae la sección
    read = []
    monkeypatch.setattr(bases_reader, "ocr_image", lambda name, data: read.append(name) or REQUIREMENTS)
    archive = make_zip({"a bases.docx": make_docx_with_images(PROCEDURE, [b"B" * 40_000]),
                        "b bases.pdf": make_pdf([filler(5), REQUIREMENTS])})
    result = bases_reader.read_bases(archive, use_ocr=True)
    assert result.section.document == "b bases.pdf" and read == []


def test_habilitaciones_heading_of_reverse_auctions():
    # Base 1262142: "15. HABILITACIONES" (página 27) con la inscripción en OSINERGMIN
    item = ("15. HABILITACIONES\nCopia simple de la autorización e inscripción vigente en el Registro de Hidrocarburos "
            "de OSINERGMIN, según el D.S. N° 004-2010-EM.\n" + "ACREDITACIÓN: copia simple de la inscripción vigente. " * 12
            + "\n16. GARANTÍA COMERCIAL\nDoce meses desde la entrega.\n")
    result = bases_reader.read_bases(make_docx(filler(5) + "\n" + item), use_ocr=False)
    assert result.status == "extraido" and result.section.text.startswith("HABILITACIONES")
    assert "GARANTÍA COMERCIAL" not in result.section.text            # termina en el siguiente numeral
    both = bases_reader.read_bases(make_docx(item + REQUIREMENTS), use_ocr=False)
    assert both.section.text.startswith("REQUISITOS DE CALIFICACIÓN")   # la sección de requisitos tiene prioridad
    assert bases_reader.clean_ocr("Texto real\n1]\n(5)\nfo]\nA.\n2)\nOtra línea") == "Texto real\nA.\n2)\nOtra línea"


def test_explain_says_why_each_line_was_discarded():
    index = "3.2 REQUISITOS DE CALIFICACIÓN .................................. 12\n"
    short = "3.6 REQUISITOS DE CALIFICACIÓN\nA. CAPACIDAD LEGAL: licencia municipal.\nCAPÍTULO IV\n" + filler(5)
    other = "3.7 CONDICIONES DE CALIFICACIÓN DEL POSTOR\n"
    report = bases_reader.explain(make_docx(index + PROCEDURE + other + short), use_ocr=False)
    verdicts = " | ".join(v for _, _, v in report[0]["lines"])
    assert "menos de ~600 caracteres" in verdicts and "índice" in verdicts
    assert "dentro de un párrafo" in verdicts and "otro título" in verdicts
    first = report[0]["lines"][0][2]
    assert first.startswith("título reconocido, pero la sección mide")      # lo más útil va primero


def test_unknown_option_is_an_error_instead_of_running_the_ingestion(monkeypatch):
    import ingest_job
    called = []
    monkeypatch.setattr(ingest_job.ingest, "run_ingestion", lambda *a, **k: called.append(1))
    with pytest.raises(SystemExit) as error:
        ingest_job.main_with(["--opcion-que-no-existe"])
    assert error.value.code == 2 and called == []


def test_bases_diagnosis_and_reread_options(tmp_path, monkeypatch, capsys):
    import ingest_job
    url = f"sqlite:///{tmp_path / 'base.db'}"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "clave-de-prueba")
    monkeypatch.setattr(llm, "embed_texts", fake_embed)
    monkeypatch.setattr(bases_download, "open_bases", lambda url, **kwargs: fake_fetch(url))
    monkeypatch.setattr(bases_reader, "ocr_available", lambda: False)
    engine = db.make_engine(url)
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    seed(factory, codes=("1001", "1003"))
    rag.index_pending(factory, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
    assert statuses(factory)["1003"] == "seccion_no_encontrada"
    assert ingest_job.main_with(["--diagnosticar-bases"]) == 0
    out = capsys.readouterr().out
    assert "=== 1003" in out and "1001" not in out and "dentro de un párrafo" in out
    assert "Documento leído: Bases Administrativas (pdf)" in out
    assert statuses(factory)["1003"] == "seccion_no_encontrada"            # el diagnóstico no guarda nada
    assert ingest_job.main_with(["--releer-sin-seccion"]) == 0
    assert "1 bases sin la sección vuelven a quedar pendientes" in capsys.readouterr().out
    engine.dispose()


# ---------------------------------------------------------------------------
# PostgreSQL con pgvector (opcional)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("RAG_TEST_POSTGRES_URL"), reason="sin base PostgreSQL de prueba")
def test_postgres_with_pgvector_end_to_end():
    from sqlalchemy import text
    engine = db.make_engine(os.environ["RAG_TEST_POSTGRES_URL"])
    db.Base.metadata.drop_all(engine)
    db.create_tables(engine)                                  # activa la extensión vector
    factory = db.make_session_factory(engine)
    try:
        seed(factory, codes=("1001", "1002"))
        rag.index_pending(factory, fetch=fake_fetch, embed=fake_embed, use_ocr=False)
        with factory() as session:
            column_type = session.execute(text("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                                               "WHERE attrelid = 'fragmentos'::regclass AND attname = 'embedding'")).scalar()
            assert column_type == f"vector({config.EMBEDDING_DIM})"
            top = rag.search(session, "monto facturado de experiencia en venta de diésel", k=2, embed=fake_embed)
            assert top[0]["code"] == "1001" and top[0]["page"] == 2 and 0 < top[0]["similarity"] <= 1
    finally:
        db.Base.metadata.drop_all(engine)
        engine.dispose()