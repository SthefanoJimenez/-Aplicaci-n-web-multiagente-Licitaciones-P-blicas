"""Texto de los PDF con pdftotext (Poppler) y pypdf como respaldo (14/11/2026).

pypdf partía palabras en los PDF de texto justificado («Decr eto», «Registro N acional»): en 4 bases
reales (1258549, 1262520, 1262142 y 1263973) dejó entre 14 y 22 palabras partidas cada una, y pdftotext
ninguna. Estas pruebas cubren cuándo se usa cada uno; no necesitan internet.
"""
import os

import pytest

from services import bases_reader
from test_en012 import make_pdf

HAS_PDFTOTEXT = bases_reader.pdftotext_path() is not None


@pytest.mark.skipif(not HAS_PDFTOTEXT, reason="Poppler (pdftotext) no está instalado")
def test_con_poppler_el_texto_sale_de_pdftotext_pagina_por_pagina():
    pages = bases_reader.poppler_pages(make_pdf(["Primera pagina", "Segunda pagina", "Tercera"]), 3)
    assert [p.strip() for p in pages] == ["Primera pagina", "Segunda pagina", "Tercera"]


def test_sin_poppler_se_usa_pypdf(monkeypatch):
    monkeypatch.setattr(bases_reader, "pdftotext_path", lambda: None)
    pages, big = bases_reader.pdf_pages_info(make_pdf(["Hola mundo", "Adios"]))
    assert [p.strip() for p in pages] == ["Hola mundo", "Adios"] and big == [False, False]


def test_si_pdftotext_da_otro_numero_de_paginas_se_usa_pypdf(monkeypatch):
    monkeypatch.setattr(bases_reader, "pdftotext_path", lambda: "pdftotext")

    class Done:
        returncode, stdout = 0, "solo una pagina\f".encode()

    monkeypatch.setattr(bases_reader.subprocess, "run", lambda *args, **kwargs: Done())
    assert bases_reader.poppler_pages(b"%PDF", 2) is None


def test_se_prefiere_pdftotext_y_una_pagina_vacia_queda_con_pypdf(monkeypatch):
    content = make_pdf(["texto de pypdf uno", "texto de pypdf dos"])
    monkeypatch.setattr(bases_reader, "poppler_pages", lambda data, expected: ["Decreto Supremo", "   "])
    pages, _ = bases_reader.pdf_pages_info(content)
    assert pages[0] == "Decreto Supremo"                 # la de pdftotext
    assert pages[1].strip() == "texto de pypdf dos"      # pdftotext no sacó nada en esta página


def test_si_pdftotext_falla_se_usa_pypdf(monkeypatch):
    monkeypatch.setattr(bases_reader, "pdftotext_path", lambda: "pdftotext")

    def broken(*args, **kwargs):
        raise OSError("no se pudo ejecutar")

    monkeypatch.setattr(bases_reader.subprocess, "run", broken)
    assert bases_reader.poppler_pages(make_pdf(["a"]), 1) is None
    pages, _ = bases_reader.pdf_pages_info(make_pdf(["Hola mundo"]))
    assert pages[0].strip() == "Hola mundo"


def test_en_windows_encuentra_pdftotext_junto_a_pdftoppm(monkeypatch, tmp_path):
    pdftoppm = tmp_path / "pdftoppm.exe"
    pdftotext = tmp_path / "pdftotext.exe"
    pdftoppm.write_bytes(b"")
    pdftotext.write_bytes(b"")
    monkeypatch.setattr(bases_reader, "tool_path", lambda name: str(pdftoppm) if name == "pdftoppm" else None)
    assert bases_reader.pdftotext_path() == os.path.join(str(tmp_path), "pdftotext.exe")