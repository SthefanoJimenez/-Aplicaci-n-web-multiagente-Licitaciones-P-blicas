"""Pruebas del script de SP-005 que no necesitan internet.

Ejecutar desde analisis/:  python -m pytest tests_sp005
(o desde la raíz del proyecto:  python -m pytest analisis/tests_sp005)
"""
import gzip
import io
import json
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

# conftest.py agrega analisis/ a la ruta; no cambiar a "from analisis import ...".
import sp005_01_tender_source as sp

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=sp.LIMA)


def release(ocid, deadline=None, status="active", category="goods", date="2026-10-01"):
    return {"ocid": ocid, "date": date,
            "tender": {"id": ocid, "mainProcurementCategory": category, "status": status,
                       "tenderPeriod": {"endDate": deadline}}}


def test_reads_zip_gzip_jsonl_and_record_packages(tmp_path):
    package = {"releases": [release("a"), release("b")]}
    zipped = tmp_path / "m.bin"
    with zipfile.ZipFile(zipped, "w") as z:
        z.writestr("data.json", json.dumps(package))
    gz = tmp_path / "g.bin"
    gz.write_bytes(gzip.compress("\n".join(json.dumps(release(x)) for x in "cd").encode()))
    records = tmp_path / "r.json"
    records.write_text(json.dumps({"records": [{"ocid": "e", "compiledRelease": release("e")}]}))
    ocids = {r["ocid"] for path in (zipped, gz, records) for r in sp.compiled_releases(sp.read_ocds_file(path))}
    assert ocids == {"a", "b", "c", "d", "e"}


def test_merges_releases_of_the_same_process_keeping_the_latest():
    early = release("x", deadline="2026-10-20T10:00:00-05:00", date="2026-10-01")
    late = {"ocid": "x", "date": "2026-10-05", "tender": {"id": "x", "status": "complete"}}
    merged = list(sp.compiled_releases([{"releases": [late, early]}]))
    assert len(merged) == 1 and merged[0]["tender"]["status"] == "complete"


@pytest.mark.parametrize("deadline, status, expected", [
    ("2026-10-20T10:00:00-05:00", "active", True),
    ("2026-10-01T10:00:00-05:00", "active", False),
    (None, "active", True),
    (None, "complete", False),
])
def test_open_means_future_deadline_or_active_without_deadline(deadline, status, expected):
    row = sp.normalize_release(release("x", deadline, status))
    assert sp.is_open(row, NOW) is expected


def test_goods_detection_accepts_spanish_and_english():
    assert sp.is_goods({"category": "goods"}) and sp.is_goods({"category": "Bienes"})
    assert not sp.is_goods({"category": "services"})


def test_department_comes_from_the_buyer_party():
    r = release("x")
    r["buyer"] = {"id": "B1", "name": "MUNICIPALIDAD"}
    r["parties"] = [{"id": "S1", "roles": ["supplier"], "address": {"region": "LIMA"}},
                    {"id": "B1", "roles": ["buyer"], "address": {"department": "LA LIBERTAD"}}]
    row = sp.normalize_release(r)
    assert row["department"] == "LA LIBERTAD" and row["department_field"] == "department"


def test_rubro_is_the_unspsc_family_with_fallback():
    with_unspsc = {"classifications": [{"scheme": "CUBSO", "id": "9999999900001"}, {"scheme": "UNSPSC", "id": "30111601"}]}
    without = {"classifications": [{"scheme": "CUBSO", "id": "1510150500012"}]}
    assert sp.family_of(with_unspsc) == "3011"
    assert sp.family_of(without) == "1510"


def test_catalog_match_by_code_or_name():
    catalog = {"codes": {12771}, "names": {sp.normalize("CEMENTO PORTLAND TIPO I X 42.50 kg")}}
    by_code = {"description": "x", "classifications": [{"scheme": "CUBSO", "id": "3011160100012771", "description": "x"}]}
    by_name = {"description": "Cemento Portland tipo I x 42.50 KG", "classifications": []}
    assert sp.in_catalog(by_code, catalog) == "code"
    assert sp.in_catalog(by_name, catalog) == "name"
    assert sp.in_catalog({"description": "otro", "classifications": []}, catalog) is None


def test_prefers_the_bases_document():
    row = {"documents": [{"title": "Resumen", "type": "tenderNotice", "url": "u1"},
                         {"title": "Bases Administrativas", "type": "biddingDocuments", "url": "u2"}]}
    assert sp.bases_document(row)["url"] == "u2"
    assert sp.bases_document({"documents": [{"title": "Bases", "url": None}]}) is None


def _text_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for line_no, line in enumerate(["CAPITULO II", "REQUISITOS DE CALIFICACION",
                                    "Experiencia del postor en la especialidad"] + ["texto %d" % i for i in range(60)]
                                   + ["", "ANEXO 1"]):
        c.drawString(40, 800 - 12 * (line_no % 60), line)
        if line_no % 60 == 59:
            c.showPage()
    c.save()
    return buf.getvalue()


def _scanned_pdf():
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), "white").save(buf, "PDF")
    return buf.getvalue()


def test_pdf_with_text_and_requirements_section():
    info = sp.analyze_pdf(_text_pdf())
    assert info["has_text"] and info["requirements_found"]
    assert 0 < info["tokens_requirements_est"] < info["tokens_full_est"]


def test_scanned_pdf_is_detected():
    info = sp.analyze_pdf(_scanned_pdf())
    assert not info["has_text"] and not info["requirements_found"]


def test_conclusion_levels():
    fields = [{"pct": 100.0}]
    good = pd.DataFrame({"tiene_enlace": [True] * 10, "descargable": [True] * 8 + [False] * 2,
                         "pdf_obtenido": [True] * 8 + [False] * 2,
                         "has_text": [True] * 7 + [False] + [None] * 2})
    assert sp.conclusion(fields, good)[0] == "VIABLE"
    partial = good.assign(descargable=[True] * 6 + [False] * 4, pdf_obtenido=[True] * 6 + [False] * 4,
                          has_text=[True] * 2 + [False] * 4 + [None] * 4)
    assert sp.conclusion(fields, partial)[0] == "VIABLE CON RESTRICCIONES"
    bad = good.assign(tiene_enlace=[False] * 10, descargable=[False] * 10, pdf_obtenido=[False] * 10,
                      has_text=[None] * 10)
    assert sp.conclusion(fields, bad)[0] == "NO VIABLE con esta fuente"


def test_months_to_fetch_crosses_the_year():
    assert sp.months_to_fetch(datetime(2026, 1, 5, tzinfo=sp.LIMA), 2) == [(2026, 1), (2025, 12)]


# --- Hallazgos de los datos reales del OECE (13/10/2026) ---------------------------

def real_like_release(start, end, item_status="CONVOCADO", amount=0.0):
    """Estructura vista en /releases: periodo de un día, valor 0 y estado en los ítems."""
    return {"ocid": "ocds-dgv273-seacev3-1", "tender": {
        "id": "1", "mainProcurementCategory": "goods",
        "tenderPeriod": {"startDate": start, "endDate": end},
        "value": {"amount": amount, "currency": "PEN"},
        "items": [{"description": "x", "status": "active", "statusDetails": item_status,
                   "classification": {"scheme": "CUBSO", "id": "7214110700233033"},
                   "additionalClassifications": [{"scheme": "UNSPSC", "id": "72141107"}]}]}}


def test_one_day_tender_period_is_not_a_deadline_and_uses_item_status():
    row = sp.normalize_release(real_like_release("2026-10-02T00:00:00-05:00", "2026-10-02T00:00:00-05:00"))
    assert row["deadline"] is None and row["tender_period_end"] == "2026-10-02T00:00:00-05:00"
    assert sp.is_open(row, NOW)
    closed = sp.normalize_release(real_like_release("2026-10-02T00:00:00-05:00", "2026-10-02T00:00:00-05:00",
                                                    item_status="ADJUDICADO"))
    assert not sp.is_open(closed, NOW)


def test_real_period_with_future_end_is_the_deadline():
    row = sp.normalize_release(real_like_release("2026-10-02T00:00:00-05:00", "2026-10-20T17:00:00-05:00"))
    assert row["deadline"] == "2026-10-20T17:00:00-05:00" and sp.is_open(row, NOW)


def test_zero_value_counts_as_missing():
    assert sp.normalize_release(real_like_release(None, None, amount=0.0))["value"] is None
    assert sp.normalize_release(real_like_release(None, None, amount=1500.0))["value"] == 1500.0


def test_cubso_code_last_eight_digits_and_unspsc_family():
    item = sp.normalize_release(real_like_release(None, None))["items"][0]
    assert sp.family_of(item) == "7214"
    assert sp.in_catalog(item, {"codes": {233033}, "names": set()}) == "code"


def test_file_kind_and_pdfs_inside_zip():
    pdf = _text_pdf()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bases.pdf", pdf)
        z.writestr("anexo.docx", b"x")
    assert sp.file_kind(pdf) == "pdf" and sp.file_kind(buf.getvalue()) == "zip"
    assert sp.file_kind(b"Rar!\x1a\x07\x00") == "rar" and sp.file_kind(b"<html>") == "otro"
    inside = sp.pdfs_inside(buf.getvalue(), "zip")
    assert [name for name, _ in inside] == ["bases.pdf", "anexo.docx"]
    info = sp.analyze_pdfs(inside)  # el .docx dañado se omite sin perder el PDF
    assert info["requirements_found"] and info["formato_documento"] == "pdf"


def test_broken_rar_raises_a_clear_error():
    with pytest.raises(RuntimeError, match="no se pudo extraer"):
        sp.pdfs_inside(b"Rar!\x1a\x07\x00basura", "rar")


def test_department_extension_as_object():
    party = {"address": {"department": {"id": "13", "name": "La Libertad"}}}
    assert sp._department(party) == ("LA LIBERTAD", "department")


def test_profile_script_reads_a_monthly_zip(tmp_path, monkeypatch):
    import sp005_02_profile_fields as profile
    r = real_like_release("2026-10-02T00:00:00-05:00", "2026-10-02T00:00:00-05:00")
    r["tender"]["documents"] = [{"documentType": "biddingDocuments", "format": "rar",
                                 "title": "Bases Administrativas", "url": "u"}]
    path = tmp_path / "2026-10_seace_v3_json.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("2026-10.json", json.dumps({"releases": [r]}))
    monkeypatch.setattr(sp, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(sys, "argv", ["x", str(path)])
    profile.main()
    report = (tmp_path / "out" / "perfil_campos.txt").read_text(encoding="utf-8")
    assert "CONVOCADO" in report and "endDate = startDate (un día)" in report and "rar" in report
    sample = json.loads((tmp_path / "out" / "muestra_bienes.json").read_text(encoding="utf-8"))
    assert sample[0]["ocid"] == "ocds-dgv273-seacev3-1"


def test_run_all_helpers():
    import sp005_00_run_all as run_all
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Bases/bases.pdf", _text_pdf())
        z.writestr("Bases/anexos.docx", b"x")
    assert run_all.file_kind(buf.getvalue()) == "zip" and run_all.file_kind(b"  <html>") == "html"
    assert run_all.file_kind(_docx(["hola"])) == "docx" and run_all.file_kind(_text_pdf()) == "pdf"
    assert run_all.list_archive(buf.getvalue(), "zip") == ["Bases/bases.pdf", "Bases/anexos.docx"]
    with pytest.raises(RuntimeError):
        run_all.list_archive(b"Rar!\x1a\x07\x00basura", "rar")
    assert run_all.first_list({"records": [1]}, "records") == [1] and run_all.first_list({"a": 1}, "records") == [{"a": 1}]
    assert run_all.api_root("https://x.pe/api/v1/") == ("https://x.pe", "https://x.pe/api/v1")
    assert run_all.api_root("https://x.pe") == ("https://x.pe", "https://x.pe/api/v1")


def test_prefers_integrated_bases_over_administrative():
    row = {"documents": [{"title": "Bases Administrativas", "type": "biddingDocuments", "url": "u1"},
                         {"title": "Informe de desierto", "type": "evaluationReports", "url": "u0"},
                         {"title": "Bases Integradas", "type": "biddingDocuments", "url": "u2"}]}
    assert sp.bases_document(row)["url"] == "u2"


def test_missing_deadline_and_value_give_viable_with_restrictions():
    rows = [sp.normalize_release(real_like_release("2026-10-02T00:00:00-05:00", "2026-10-02T00:00:00-05:00"))
            for _ in range(10)]
    for r in rows:
        r.update(code="1", entity="E", department="LIMA", published="2026-10-02T00:00:00-05:00")
    fields = sp.field_coverage(rows)
    by_name = {f["campo"]: f for f in fields}
    assert by_name["fecha límite de ofertas"]["pct"] == 0 and by_name["código"]["tipo"] == "esencial"
    docs = pd.DataFrame({"tiene_enlace": [True] * 10, "descargable": [True] * 10,
                         "pdf_obtenido": [True] * 10, "has_text": [True] * 10})
    assert sp.conclusion(fields, docs)[0] == "VIABLE CON RESTRICCIONES"


def test_age_profile_of_open_tenders():
    rows = [{"published": (NOW - timedelta(days=d)).isoformat()} for d in (1, 10, 45)] + [{"published": None}]
    assert sp.age_profile(rows, NOW) == {"con_fecha": 3, "hasta_7": 1, "8_a_30": 1, "mas_de_30": 1}


def _docx(paragraphs):
    buf = io.BytesIO()
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_word_bases_are_read_as_text():
    content = _docx(["CAPITULO III", "REQUISITOS DE CALIFICACION", "Experiencia del postor &amp; facturacion"]
                    + ["linea %d" % i for i in range(40)] + ["ANEXO 1"])
    assert sp.file_kind(content) == "docx"
    info = sp.analyze_pdfs(sp.pdfs_inside(content, "docx"))
    assert info["formato_documento"] == "docx" and info["has_text"] and info["requirements_found"]
    assert info["pages"] == 0 and info["tokens_gemini_pdf_est"] == 0


def test_scanned_pdf_gets_gemini_page_estimate():
    info = sp.analyze_pdfs([("b.pdf", _scanned_pdf())])
    assert not info["has_text"] and info["tokens_gemini_pdf_est"] == sp.GEMINI_TOKENS_PER_PDF_PAGE


def test_sample_takes_at_most_two_per_entity():
    rows = [{"entity": e} for e in ["A", "A", "A", "B", "A", "C"]]
    assert [r["entity"] for r in sp._spread_sample(rows, 4)] == ["A", "A", "B", "C"]


def test_rubro_rule_prefers_volume_among_well_covered():
    table = pd.DataFrame([
        {"rubro": "5512", "vigentes": 12, "vigentes_la_libertad": 3, "cobertura_catalogo_pct": 100.0},
        {"rubro": "1510", "vigentes": 151, "vigentes_la_libertad": 4, "cobertura_catalogo_pct": 99.5},
        {"rubro": "2510", "vigentes": 59, "vigentes_la_libertad": 7, "cobertura_catalogo_pct": 92.2},
        {"rubro": "4111", "vigentes": 97, "vigentes_la_libertad": 7, "cobertura_catalogo_pct": 61.8},
        {"rubro": "3011", "vigentes": 88, "vigentes_la_libertad": 0, "cobertura_catalogo_pct": 96.7},
    ])
    assert sp.choose_rubros(table) == ["1510", "2510"]


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_run_all_end_to_end_with_simulated_api(tmp_path):
    """Partes B, C y D completas contra la API simulada, sin tocar salidas/ ni la caché reales."""
    import os
    import socket
    import subprocess
    import time
    here = Path(__file__).resolve().parent
    port = _free_port()
    server = subprocess.Popen([sys.executable, str(here / "fake_ocds_server.py"), str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):  # esperar a que el servidor acepte conexiones
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        import urllib.request
        try:
            version = urllib.request.urlopen(f"http://127.0.0.1:{port}/__version__", timeout=5).read().decode()
        except OSError:
            version = ""  # un servidor anterior no tiene /__version__
        assert version[:4].isdigit() and version >= "2026-10-17", (
            "tests_sp005/fake_ocds_server.py está desactualizado: reemplácelo por la versión final")
        catalog = tmp_path / "catalog.csv"
        catalog.write_text("codigoitem,itemcubso\n1,DIESEL B5 S-50\n", encoding="utf-8")  # solo combustibles: rubro 1510
        # PYTHONIOENCODING=cp1252 simula la consola de Windows, donde "→" no existe (error real del 17/10/2026)
        env = os.environ | {"SP005_OUTPUT_DIR": str(tmp_path / "salidas"), "SP005_CACHE_DIR": str(tmp_path / "cache"),
                            "PYTHONIOENCODING": "cp1252"}
        result = subprocess.run(
            [sys.executable, str(here.parent / "sp005_00_run_all.py"), "--partes", "B,C,D", "--sample", "6",
             "--catalog", str(catalog), "--base-url", f"http://127.0.0.1:{port}/api/v1"],
            capture_output=True, encoding="cp1252", errors="replace", env=env, timeout=240)
    finally:
        server.terminate()
        server.wait()
    summary = (tmp_path / "salidas" / "resumen_a_d.md").read_text(encoding="utf-8")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert "FALLÓ" not in summary, summary
    assert "| B0 Identificación | OK | sin identificar 403 · identificado 200 |" in summary, summary
    assert "conclusión: VIABLE" in summary
    assert "UnicodeEncodeError" not in result.stdout + result.stderr
    assert (tmp_path / "salidas" / "informe_sp005.md").exists() and (tmp_path / "salidas" / "demo_bases.pdf").exists()
    docs = pd.read_csv(tmp_path / "salidas" / "documentos.csv")
    assert {"docx", "zip", "pdf"} <= set(docs["formato_real"].dropna())


def test_console_without_utf8_does_not_crash():
    """En una consola cp1252 (Windows), imprimir "→" no debe detener el script."""
    import subprocess
    code = ("import sp005_01_tender_source as sp; sp.safe_console(); print('pdf → pdf · con texto')")
    here = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", code], cwd=here, capture_output=True,
                            env={**__import__("os").environ, "PYTHONIOENCODING": "cp1252"})
    assert result.returncode == 0, result.stderr.decode(errors="replace")