"""Pruebas de los scripts de SP-006. No usan internet ni la API de Gemini."""
import io
import json
import sys
import zipfile
from types import SimpleNamespace

import pandas as pd
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

# conftest.py agrega analisis/ a la ruta; no cambiar a "from analisis import ...".
import sp006_01_leer_bases as lb
import sp006_02_gemini_escaneadas as gm
import sp006_03_comparar_meses as cm
import sp006_04_recuperar_bases as rb


def _pdf(pages):
    """PDF con texto: una lista de páginas, cada una una lista de líneas."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for lines in pages:
        for n, line in enumerate(lines):
            c.drawString(40, 800 - 12 * n, line)
        c.showPage()
    c.save()
    return buf.getvalue()


BASE_PAGES = [
    ["INDICE", "Requisitos de calificacion .......... 12", "CAPITULO II"],
    ["CAPITULO I", "Valor estimado: S/ 120,500.00 incluido IGV", "Cronograma del procedimiento",
     "Presentacion de ofertas 05/11/2026"] + [f"texto {i}" for i in range(30)],
    ["CAPITULO III", "REQUISITOS DE CALIFICACION", "A. Habilitacion", "B. Experiencia del postor"]
    + [f"facturacion de bienes iguales o similares {i}" for i in range(40)],
    ["", "ANEXO 1", "Declaracion jurada"],
]


def _docx(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body>" + "".join(
            f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n")) + "</w:body></w:document>")
    return buf.getvalue()


def _scanned_pdf():
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), "white").save(buf, "PDF")
    return buf.getvalue()


def test_rar_version_by_signature():
    assert lb.rar_version(b"Rar!\x1a\x07\x01\x00resto") == "RAR5"
    assert lb.rar_version(b"Rar!\x1a\x07\x00resto") == "RAR4"
    assert lb.rar_version(b"PK\x03\x04") == "desconocido"


def test_keeps_the_longest_section_not_the_index_entry():
    pages = [p.extract_text() for p in __import__("pypdf").PdfReader(io.BytesIO(_pdf(BASE_PAGES))).pages]
    text, starts = lb.join_pages(pages)
    sections = lb.requirement_sections(text)
    assert len(sections) == 2                       # la línea del índice y la sección real
    assert "Experiencia del postor" in sections[0]["text"]
    assert lb.page_of(sections[0]["start"], starts) == 3
    assert "ANEXO" not in sections[0]["text"]


def test_prefers_a_heading_over_a_long_paragraph_that_mentions_it():
    text = ("1.2 Las ofertas se evalúan con los requisitos de calificación exigidos en las bases, "
            + "texto del párrafo " * 300 + "\nCAPÍTULO III\n"
            + "3.6 REQUISITOS DE CALIFICACIÓN\nA. CAPACIDAD LEGAL\nRegistro de Hidrocarburos\n" + "detalle " * 60
            + "\nCAPÍTULO IV\n")
    sections = lb.requirement_sections(text)
    assert sections[0]["titulo"] and sections[0]["text"].startswith("REQUISITOS DE CALIFICACIÓN\nA. CAPACIDAD")
    assert not sections[1]["titulo"] and len(sections[1]["text"]) > len(sections[0]["text"])
    assert lb.is_heading("CAPÍTULO III: REQUISITOS", len("CAPÍTULO III: "))
    # Párrafo cortado en líneas (como sale del OCR): no es un título
    wrapped = "La evaluación de los\nrequisitos de calificación se realiza posteriormente"
    assert not lb.is_heading(wrapped, wrapped.index("requisitos"))
    assert lb.is_heading("x\n3.6.1 Requisitos de calificación obligatorios", len("x\n3.6.1 "))


def test_heading_with_stray_spaces_from_pdf_extraction():
    text = ("La revisión de los requisitos de calificación se realiza después.\n" + "x " * 400
            + "\n3.6 R EQUISITOS DE CALIFICACIÓ N\nA. CAPACIDAD LEGAL\n" + "detalle " * 60 + "\nCAPÍTULO IV\n")
    sections = lb.requirement_sections(text)
    assert sections[0]["titulo"] and "CAPACIDAD LEGAL" in sections[0]["text"]


def test_only_a_paragraph_mention_is_not_a_useful_section():
    pdf = _pdf([["La revision de los requisitos de calificacion se realiza despues de la evaluacion"]
                + [f"texto del procedimiento {i}" for i in range(45)]])
    info, _ = lb.analyze_document("bases.pdf", pdf, use_ocr=False)
    assert info["seccion_encontrada"] and not info["seccion_es_titulo"] and not info["seccion_util"]


def test_finds_value_with_amount_and_offer_date():
    info, section = lb.analyze_document("bases.pdf", _pdf(BASE_PAGES), use_ocr=False)
    assert info["valor_con_monto"] and "120,500.00" in info["valor_fragmento"] and info["valor_pagina"] == 2
    assert info["presentacion_ofertas_con_fecha"] and info["ofertas_pagina"] == 2
    assert info["seccion_encontrada"] and info["seccion_pagina"] == 3 and not info["seccion_corta"]
    assert info["apariciones_requisitos"] == 2 and "Habilitacion" in section


def test_short_section_is_flagged_as_probable_index():
    info, _ = lb.analyze_document("bases.pdf", _pdf([["Requisitos de calificacion ..... 12", "CAPITULO II"]]
                                                     + [["CAPITULO II", "otra cosa"]]), use_ocr=False)
    assert info["seccion_encontrada"] and info["seccion_corta"]


def test_word_and_scanned_documents():
    info, _ = lb.analyze_document("bases.docx", _docx("CAPITULO III\nREQUISITOS DE CALIFICACION\n" +
                                                      "\n".join(f"experiencia {i}" for i in range(60))), False)
    assert info["formato_documento"] == "docx" and info["seccion_encontrada"] and info["seccion_pagina"] is None
    info, _ = lb.analyze_document("escaneo.pdf", _scanned_pdf(), use_ocr=False)
    assert info["escaneado"] and not info["ocr_usado"] and not info["seccion_encontrada"]
    assert info["tokens_gemini_pdf_est"] == 258


def test_rar_falls_back_to_the_next_tool_and_ignores_empty_files(monkeypatch):
    pdf = _pdf(BASE_PAGES)
    empty = (lambda src, dst: [sys.executable, "-c", f"open(r'{dst}/bases.pdf','wb').close()"])
    good = (lambda src, dst: [sys.executable, "-c",
                              f"import shutil; shutil.copy(r'{src}', r'{dst}/bases.pdf')"])
    monkeypatch.setattr(lb, "extractors", lambda: [("7z", empty), ("unar", good)])
    files, tool = lb.documents_in(pdf, "rar")          # el "rar" de prueba es el mismo PDF copiado
    assert tool == "unar" and files[0][0] == "bases.pdf" and files[0][1] == pdf


def test_rar_without_tools_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(lb, "extractors", lambda: [])
    with pytest.raises(RuntimeError, match="no hay bsdtar, unar ni 7z"):
        lb.documents_in(b"Rar!\x1a\x07\x01\x00", "rar")


def _fake_sp005(tmp_path):
    folder = tmp_path / "sp005"
    (folder / "bases").mkdir(parents=True)
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as z:
        z.writestr("bases.pdf", _pdf(BASE_PAGES))
    files = {"ocds-1": ("pdf", _pdf(BASE_PAGES)), "ocds-2": ("docx", _docx("REQUISITOS DE CALIFICACION\nnada")),
             "ocds-3": ("zip", zipped.getvalue()), "ocds-4": ("pdf", _scanned_pdf())}
    rows, vigentes = [], []
    for n, (ocid, (kind, content)) in enumerate(files.items(), start=1):
        (folder / "bases" / f"ocds-{n}.{kind}").write_bytes(content)
        rows.append({"ocid": ocid, "codigo": str(100 + n), "entidad": f"E{n}", "documento": "Bases Administrativas",
                     "formato_real": kind, "pages": 1})
        vigentes.append({"ocid": ocid, "method": "Subasta Inversa Electrónica", "items": [
            {"description": "DIESEL", "classifications": [{"scheme": "UNSPSC", "id": "15101505", "description": "x"}]}]})
    rows.append({"ocid": "ocds-9", "codigo": "109", "entidad": "E9", "documento": "Bases", "formato_real": "pdf", "pages": 1})
    pd.DataFrame(rows).to_csv(folder / "documentos.csv", index=False)
    (folder / "vigentes.json").write_text(json.dumps(vigentes), encoding="utf-8")
    return folder


def test_read_command_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lb, "SP005_DIR", _fake_sp005(tmp_path))
    monkeypatch.setattr(lb, "OUTPUT_DIR", tmp_path / "sp006")
    lb.command_read(SimpleNamespace(codigos="101,102,103,104,109", ocr=False))
    table = pd.read_csv(tmp_path / "sp006" / "bases_sp006.csv", dtype={"codigo": str, "rubros": str})
    assert list(table["codigo"]) == ["101", "102", "103", "104", "109"]
    assert table.loc[0, "rubros"] == "1510" and table.loc[0, "metodo"] == "Subasta Inversa Electrónica"
    assert table.loc[2, "abierto_con"] == "python" and bool(table.loc[2, "seccion_encontrada"])
    assert bool(table.loc[3, "escaneado"]) and "No está el archivo" in table.loc[4, "error"]
    assert (tmp_path / "sp006" / "secciones" / "101.txt").read_text(encoding="utf-8").startswith("REQUISITOS")
    assert int(table.loc[0, "documentos_en_archivo"]) == 1 and bool(table.loc[0, "seccion_util"])
    catalog = tmp_path / "sp006" / "catalogo_requisitos.csv"
    assert list(pd.read_csv(catalog).columns) == lb.CATALOG_COLUMNS
    catalog.write_text("tipo_requisito\nya llenado\n", encoding="utf-8")
    lb.command_read(SimpleNamespace(codigos="101", ocr=False))      # no pisa el catálogo del equipo
    assert "ya llenado" in catalog.read_text(encoding="utf-8")
    again = pd.read_csv(tmp_path / "sp006" / "bases_sp006.csv", dtype={"codigo": str})
    assert sorted(again["codigo"]) == ["101", "102", "103", "104", "109"]   # no pierde las demás bases
    assert "Bases leídas: 4 de 5" in capsys.readouterr().out


def _row(ocid, family, published="2026-10-01T10:00:00-05:00"):
    return {"ocid": ocid, "code": ocid, "method": "Licitación Pública", "published": published, "entity": "E",
            "department": "LIMA", "items": [{"description": "x", "classifications": [
                {"scheme": "UNSPSC", "id": family + "0000", "description": "x"}]}]}


def test_third_month_comparison():
    two = {"a": _row("a", "1510"), "b": _row("b", "4111")}
    three = dict(two) | {"c": _row("c", "2510", "2026-08-20T10:00:00-05:00"), "d": _row("d", "3011")}
    result = cm.compare(two, three)
    assert result["piloto_2_meses"] == 1 and result["piloto_3_meses"] == 2
    assert result["aporta_tercer_mes"] == 2 and result["aporta_tercer_mes_piloto"] == 1
    assert result["dejan_de_estar_vigentes"] == 0


class _FakeModels:
    def count_tokens(self, model, contents):
        return SimpleNamespace(total_tokens=1234)

    def generate_content(self, model, contents, config):
        usage = SimpleNamespace(prompt_token_count=1300, candidates_token_count=90, thoughts_token_count=None)
        text = '```json\n{"requisitos": [{"tipo": "Experiencia", "texto": "S/ 100,000", "pagina": 12}],' \
               ' "valor_estimado": null, "fecha_presentacion_ofertas": "05/11/2026"}\n```'
        return SimpleNamespace(text=text, usage_metadata=usage)


def test_gemini_count_only_does_not_call_generation(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    client = SimpleNamespace(models=_FakeModels())
    row, parsed = gm.run_one(client, "modelo", "1255745", "pdf", ["archivo"], extract=False, pages=62)
    assert row["tokens_entrada_conteo"] == 1234 and parsed is None and "tokens_salida" not in row


def test_gemini_extraction_records_real_tokens(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    client = SimpleNamespace(models=_FakeModels())
    row, parsed = gm.run_one(client, "modelo", "1255745", "ocr", ["texto"], extract=True, pages=62)
    assert row["tokens_entrada_real"] == 1300 and row["tokens_salida"] == 90
    assert row["json_valido"] and row["requisitos_extraidos"] == 1 and row["fecha_presentacion_ofertas"] == "05/11/2026"
    assert parsed["requisitos"][0]["pagina"] == 12


def test_finds_ocr_tools_by_environment_variable_and_checks_spanish(tmp_path, monkeypatch):
    fake = tmp_path / "tesseract"
    fake.write_text("x")
    monkeypatch.setenv("SP006_TESSERACT", str(fake))
    assert lb.find_tool("tesseract", "SP006_TESSERACT", []) == str(fake)
    monkeypatch.delenv("SP006_TESSERACT")
    monkeypatch.setattr(lb.shutil, "which", lambda name: None)
    assert lb.find_tool("tesseract", "SP006_TESSERACT", [str(tmp_path / "no-existe*")]) is None
    monkeypatch.setattr(lb, "find_tool", lambda name, env, guesses: name)
    monkeypatch.setattr(lb.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="List of languages\neng\nosd\n", stderr=""))
    tools, reason = lb.ocr_tools()
    assert tools is None and "spa" in reason


def test_ocr_degree_sign_is_turned_back_into_o_but_numbers_keep_it():
    raw = "Requisitºs:\nEl pºstºr debe acreditar, º (ii) cºmprºbantes; nº es pºsible. Anexº Nº 11 y Nº09—2026. 1º De acuerdº"
    clean = lb.normalize_ocr(raw)
    assert clean == ("Requisitos:\nEl postor debe acreditar, o (ii) comprobantes; no es posible. "
                     "Anexo Nº 11 y Nº09—2026. 1º De acuerdo")
    assert lb.requirement_sections("3.5.1 REQUISITOS DE CALIFICACIÓN\n" + clean)


def test_habilitation_heading_of_a_technical_sheet_counts_as_the_section():
    text = ("Pº 10\nREQUISITOS DE HABILITACIÓN\n1. Copia simple de la autorización e inscripción vigente en el "
            "Registro de Hidrocarburos de OSINERGMIN. " * 3 + "\n")
    sections = lb.requirement_sections(text)
    assert sections and sections[0]["titulo"] and sections[0]["text"].startswith("REQUISITOS DE HABILITACIÓN")


def test_rotated_scan_is_read_again_with_orientation_detection(monkeypatch, tmp_path):
    garbage = "sasopaencid so| 10d eanjoa| el epidul ezo] op oueuser o od je SAS3NA 03 SOI! sa"
    good = "Copia simple de la autorización e inscripción vigente en el Registro de Hidrocarburos"
    calls = []

    def fake_run(cmd, **kwargs):
        if "pdftoppm" in cmd[0]:
            (lb.Path(cmd[-1]).parent / "pg-1.pgm").write_bytes(b"P5")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        calls.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout=good if cmd[-1] == "1" else garbage, stderr="")

    monkeypatch.setattr(lb, "ocr_tools", lambda: (("pdftoppm", "tesseract"), None))
    monkeypatch.setattr(lb.subprocess, "run", fake_run)
    assert lb.ocr_pdf(b"%PDF") == [good] and calls == ["3", "1"]
    assert lb.spanish_score(garbage) < lb.ROTATED_PAGE_SCORE <= lb.spanish_score(good)



def _release_row(code, docs):
    return {"ocid": f"ocds-x-{code}", "code": code, "documents": docs}


def test_recovery_keeps_the_same_bases_as_on_17_october():
    docs = [{"title": "Bases Integradas", "url": "u1"}, {"title": "Bases Administrativas", "url": "u2"}]
    row, note = rb.same_document_as_17oct(_release_row("1258393", docs), "1258393")   # el 17/10 eran Administrativas
    assert [d["url"] for d in row["documents"]] == ["u2"] and note == ""
    row, note = rb.same_document_as_17oct(_release_row("1258393", docs[:1]), "1258393")
    assert row["documents"] == docs[:1] and "Bases Integradas" in note
    assert rb.same_document_as_17oct(_release_row("1258393", []), "1258393")[1] == "sin documentos con enlace"


def test_recovery_merges_rows_and_backs_up_once(tmp_path):
    pd.DataFrame({"ocid": ["ocds-x-1", "ocds-x-2"], "codigo": ["1", "2"], "documento": ["viejo", "viejo"]}) \
        .to_csv(tmp_path / "documentos.csv", index=False, encoding="utf-8-sig")
    (tmp_path / "vigentes.json").write_text(json.dumps([{"ocid": "ocds-x-1"}, {"ocid": "ocds-x-2"}]), encoding="utf-8")
    measured = pd.DataFrame({"ocid": ["ocds-x-2", "ocds-x-3"], "codigo": ["2", "3"], "documento": ["nuevo", "nuevo"]})
    rows = {"2": {"ocid": "ocds-x-2", "method": "m"}, "3": {"ocid": "ocds-x-3", "method": "m"}}
    assert rb.merge_outputs(tmp_path, measured, rows) == (3, 3)
    docs = pd.read_csv(tmp_path / "documentos.csv", dtype={"codigo": str}).set_index("codigo")["documento"]
    assert docs.to_dict() == {"1": "viejo", "2": "nuevo", "3": "nuevo"}
    assert (tmp_path / "documentos_antes_sp006_04.csv").exists()
    rb.merge_outputs(tmp_path, measured, rows)   # la copia de respaldo no se pisa
    backup = pd.read_csv(tmp_path / "documentos_antes_sp006_04.csv", dtype={"codigo": str})
    assert list(backup["documento"]) == ["viejo", "viejo"]


def test_recovery_end_to_end_with_simulated_api(tmp_path):
    import os
    import socket
    import subprocess
    import time
    from pathlib import Path
    here = Path(__file__).resolve().parent.parent
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen([sys.executable, str(here / "tests_sp005" / "fake_ocds_server.py"), str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        out = tmp_path / "sp005"
        out.mkdir()
        pd.DataFrame({"ocid": ["otra"], "codigo": ["999"]}).to_csv(out / "documentos.csv", index=False)
        env = os.environ | {"SP005_OUTPUT_DIR": str(out), "SP005_CACHE_DIR": str(tmp_path / "cache")}
        result = subprocess.run(
            [sys.executable, str(here / "sp006_04_recuperar_bases.py"), "--codigos", "SIE-14-2026,SIE-1-2026,NO-EXISTE",
             "--mensual", "--base-url", f"http://127.0.0.1:{port}/api/v1"],
            capture_output=True, text=True, env=env, timeout=240)
        # Con los archivos mensuales bajados a mano (aquí, los de la caché): no consulta la API del mes
        months = sorted(str(p) for p in (tmp_path / "cache").glob("ocds_*.bin"))
        from_file = subprocess.run(
            [sys.executable, str(here / "sp006_04_recuperar_bases.py"), "--codigos", "SIE-1-2026",
             "--from-file", *months, "--base-url", "http://127.0.0.1:1/api/v1"],
            capture_output=True, text=True, env=env, timeout=240)
    finally:
        server.terminate()
        server.wait()
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert "NO-EXISTE" in result.stdout and "Bases recuperadas: 2 de 3" in result.stdout
    assert from_file.returncode == 0 and len(months) == 2, from_file.stdout[-2000:] + from_file.stderr[-2000:]
    assert "Descargando 1 bases" in from_file.stdout   # el documento sí se baja (de su propia dirección)
    docs = pd.read_csv(out / "documentos.csv", dtype={"codigo": str})
    assert set(docs["codigo"]) == {"999", "SIE-14-2026", "SIE-1-2026"}
    assert len(list((out / "bases").iterdir())) == 2
    vigentes = json.loads((out / "vigentes.json").read_text(encoding="utf-8"))
    assert {v["code"] for v in vigentes} == {"SIE-14-2026", "SIE-1-2026"}


def test_read_a_manually_downloaded_file_without_sp005_outputs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lb, "SP005_DIR", tmp_path / "no-existe")          # sin documentos.csv
    monkeypatch.setattr(lb, "OUTPUT_DIR", tmp_path / "sp006")
    archive = tmp_path / "bases descargadas.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("EETT.pdf", _pdf(BASE_PAGES))
    lb.command_read(SimpleNamespace(codigos=None, ocr=False, archivo=[f"1258127={archive}", f"1258420={tmp_path / 'x.pdf'}"]))
    table = pd.read_csv(tmp_path / "sp006" / "bases_sp006.csv", dtype={"codigo": str})
    assert list(table["codigo"]) == ["1258127", "1258420"]
    assert table.loc[0, "formato_descarga"] == "zip" and bool(table.loc[0, "seccion_util"])
    assert table.loc[0, "archivo_medido"] == "EETT.pdf" and "descargado a mano" in table.loc[0, "documento"]
    assert "No está el archivo" in table.loc[1, "error"]
    assert (tmp_path / "sp006" / "secciones" / "1258127.txt").exists()
    with pytest.raises(SystemExit):
        list(lb.rows_from_files(["sin-ruta"]))


def test_read_with_no_matching_codes_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lb, "SP005_DIR", _fake_sp005(tmp_path))
    monkeypatch.setattr(lb, "OUTPUT_DIR", tmp_path / "sp006")
    lb.command_read(SimpleNamespace(codigos="101", ocr=False))
    lb.command_read(SimpleNamespace(codigos="999", ocr=False))           # antes fallaba al juntar con el CSV previo
    out = capsys.readouterr().out
    assert "parece de otra ejecución" in out and "No se escribió nada" in out
    assert list(pd.read_csv(tmp_path / "sp006" / "bases_sp006.csv", dtype={"codigo": str})["codigo"]) == ["101"]


def test_recovery_by_record_uses_one_small_query_per_base(monkeypatch):
    monkeypatch.setattr(rb.time, "sleep", lambda s: None)
    asked = []
    release = {"ocid": "ocds-dgv273-seacev3-2026-1894-9", "date": "2026-10-10",
               "tender": {"id": "1253677", "procurementMethodDetails": "Licitación Pública Abreviada",
                          "documents": [{"title": "Bases Integradas", "url": "u"}], "items": []}}

    class Response:
        status_code = 200
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"records": [{"ocid": release["ocid"], "releases": [release], "compiledRelease": release}]}

    session = SimpleNamespace(base_url="https://api", get=lambda url, timeout=None, **kwargs: asked.append(url) or Response())
    rows = rb.rows_by_record(session, ["1253677"])
    assert asked == ["https://api/api/v1/record/ocds-dgv273-seacev3-2026-1894-9"]   # una consulta, con el OCID del 17/10
    assert rows["1253677"]["code"] == "1253677" and rows["1253677"]["method"] == "Licitación Pública Abreviada"


def test_archive_prefers_a_short_heading_over_a_long_mention_and_empty_csv_is_ignored(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lb, "SP005_DIR", tmp_path / "no-existe")
    monkeypatch.setattr(lb, "OUTPUT_DIR", tmp_path / "sp006")
    (tmp_path / "sp006").mkdir()
    (tmp_path / "sp006" / "bases_sp006.csv").write_text("", encoding="utf-8")       # vacío, como en la Mac el 28/10
    bases = _pdf([["La revision de los requisitos de calificacion se realiza despues"]
                  + [f"texto del procedimiento {i}" for i in range(45)]])
    eett = _pdf([["ESPECIFICACIONES TECNICAS"], ["REQUISITOS DE CALIFICACION", "A. Copia de la inscripcion en OSINERGMIN"]])
    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("BASES.pdf", bases)
        z.writestr("EETT.pdf", eett)
    lb.command_read(SimpleNamespace(codigos=None, ocr=False, archivo=[f"1258127={archive}"]))
    row = pd.read_csv(tmp_path / "sp006" / "bases_sp006.csv", dtype={"codigo": str}).iloc[0]
    assert row["archivo_medido"] == "EETT.pdf" and bool(row["seccion_es_titulo"]) and bool(row["seccion_corta"])
    assert (tmp_path / "sp006" / "secciones" / "1258127_corta.txt").exists()
    assert "título corto en p. 2" in capsys.readouterr().out


def test_heading_after_ocr_table_borders_and_index_leaders():
    text = "texto\n-) | 5.19 REQUISITOS DE HABILITACIÓN\n$00 !\na) Copia simple de la autorización OSINERGMIN " + "x " * 400
    assert lb.is_heading(text, text.index("REQUISITOS"))
    border_only = "texto\n| requisitos de calificación se revisan después"     # borde de tabla sin numeración ni mayúsculas
    assert not lb.is_heading(border_only, border_only.index("requisitos"))
    info, _ = lb.analyze_document("bases.pdf", _pdf([["3.6 REQUISITOS DE CALIFICACION .................... 25"]
                                                     + [f"3.{i} OTRO TITULO ........ {i}" for i in range(7, 40)]]), False)
    assert info["seccion_es_titulo"] and info["seccion_corta"] and not info["seccion_util"]   # índice largo, con puntos guía


def test_gemini_only_ocr_path_does_not_upload_and_keeps_earlier_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    out = tmp_path / "sp006"
    (out / "secciones").mkdir(parents=True)
    (out / "secciones" / "1255745.txt").write_text("REQUISITOS DE CALIFICACIÓN\nA. Capacidad legal", encoding="utf-8")
    pd.DataFrame([{"codigo": "1255745", "camino": "pdf", "tokens_entrada_conteo": 34721}]).to_csv(out / "gemini_escaneadas.csv", index=False)
    monkeypatch.setattr(gm, "OUTPUT_DIR", out)
    monkeypatch.setattr(gm, "load_key", lambda: ("clave", "modelo"))
    monkeypatch.setattr(gm.lb, "load_sp005", lambda: (pd.DataFrame([{"codigo": "1255745", "ocid": "x", "formato_real": "pdf",
                                                                          "pages": 62}]), {}))
    monkeypatch.setattr(gm, "upload_pdf", lambda client, path: (_ for _ in ()).throw(AssertionError("no debe subir el PDF")))
    import google.genai
    monkeypatch.setattr(google.genai, "Client", lambda api_key: SimpleNamespace(models=_FakeModels(), files=None))
    monkeypatch.setattr(sys, "argv", ["sp006_02", "--codigos", "1255745", "--extraer", "--caminos", "ocr"])
    gm.main()
    table = pd.read_csv(out / "gemini_escaneadas.csv", dtype={"codigo": str})
    assert sorted(table["camino"]) == ["ocr", "pdf"]                       # el PDF medido antes se conserva
    assert int(table.loc[table["camino"] == "pdf", "tokens_entrada_conteo"].iloc[0]) == 34721
    assert (out / "gemini" / "1255745_ocr.json").exists()