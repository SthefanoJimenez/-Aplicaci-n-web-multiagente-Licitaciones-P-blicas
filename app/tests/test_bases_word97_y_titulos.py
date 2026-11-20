"""Lectura de bases: Word 97-2003 (.doc) y títulos con puntos repetidos (20/11/2026).

Diagnóstico de la ingesta del 20/11/2026 (--diagnosticar-bases):
- 1265987 publica sus bases como Word 97-2003 (.doc) y no se podía abrir: salía «no legible».
- 1266149 titula «XVI... REQUISITOS DE CALIFICACIÓN OBLIGATORIOS» (página 34) y el título no se reconocía
  por los tres puntos tras el numeral: salía «sección no encontrada».
datos_bases_word97.doc es un Word 97-2003 hecho con LibreOffice con el mismo texto de requisitos de test_en012.
"""
import io
import shutil
import zipfile
from pathlib import Path

import pytest

from services import bases_download, bases_reader

WORD97 = (Path(__file__).parent / "datos_bases_word97.doc").read_bytes()
HAS_CONVERTER = any(shutil.which(c) for c in ("textutil", "antiword", "soffice", "libreoffice"))


@pytest.mark.parametrize("line, expected", [
    ("XVI... REQUISITOS DE CALIFICACIÓN OBLIGATORIOS", True),        # 1266149, página 34
    ("XVI. REQUISITOS DE CALIFICACIÓN", True),
    ("3.2.. REQUISITOS DE CALIFICACIÓN", True),
    ("10,1 REQUISITOS DE CALIFICACIÓN", True),                       # OCR (1262730)
    ("las bases y los requisitos de calificación, exigidos en la", False),   # mención en un párrafo
    ("2.2.1.2. Documentos para acreditar los requisitos de calificación", False),
])
def test_titulos_de_la_seccion_de_requisitos(line, expected):
    text = "Texto anterior\n" + line + "\n"
    start = bases_reader.REQUIREMENTS_PATTERN.search(text).start()
    assert bases_reader.is_heading(text, start) is expected


def test_el_word_97_se_reconoce_suelto_y_dentro_de_un_zip():
    assert bases_download.detect_kind(WORD97[:8192]) == "doc"
    assert bases_reader.documents_in(WORD97) == [("bases.doc", WORD97)]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Bases Integradas.DOC", WORD97)
    assert bases_reader.documents_in(buffer.getvalue()) == [("Bases Integradas.DOC", WORD97)]


@pytest.mark.skipif(not HAS_CONVERTER, reason="esta computadora no tiene textutil, antiword ni LibreOffice")
def test_el_word_97_se_lee_y_trae_la_seccion():
    result = bases_reader.read_bases(WORD97, use_ocr=False)
    assert result.status == "extraido"
    assert "Registro de Hidrocarburos" in result.section.text and "S/ 150,000.00" in result.section.text


def test_sin_convertidor_el_word_97_queda_no_legible(monkeypatch):
    monkeypatch.setattr(bases_reader.shutil, "which", lambda name: None)
    assert bases_reader.legacy_word_text(WORD97) is None
    assert bases_reader.read_bases(WORD97, use_ocr=False).status == "no_legible"


def test_releer_sin_seccion_tambien_vuelve_a_leer_las_no_legibles(tmp_path, monkeypatch, capsys):
    import config
    import ingest_job
    from services import db
    url = f"sqlite:///{tmp_path / 'base.db'}"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setattr(ingest_job, "run_rag", lambda factory, codes=None: ["(lectura simulada)"])
    monkeypatch.setattr(ingest_job, "run_schedules", lambda factory, limit=None: ["(cronogramas simulados)"])
    engine = db.make_engine(url)
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    with factory.begin() as session:
        for code, status in (("1", "seccion_no_encontrada"), ("2", "no_legible"), ("3", "extraido")):
            session.add(db.Tender(ocid=f"ocds-x-{code}", code=code, title="X", entity="E", state="vigente",
                                  requirements_status=status))
    assert ingest_job.main_with(["--releer-sin-seccion"]) == 0
    assert "2 bases sin la sección o no legibles vuelven a quedar pendientes" in capsys.readouterr().out
    with factory() as session:
        assert {t.code: t.requirements_status for t in session.query(db.Tender)} == {
            "1": "pendiente", "2": "pendiente", "3": "extraido"}
    engine.dispose()