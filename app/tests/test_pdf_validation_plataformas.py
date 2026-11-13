"""HU-015: la revisión aislada de PDF funciona en Linux, macOS y Windows.

En macOS, resource.setrlimit(RLIMIT_AS, ...) lanza ValueError. Antes eso hacía terminar el
validador con error y toda carga de PDF respondía 400 («No se pudo revisar el PDF»).
"""
import io
import subprocess
import sys
from pathlib import Path

from pypdf import PdfWriter

from services import pdf_validation

VALIDATOR = Path(pdf_validation.__file__)


def blank_pdf(pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class FakeResource:
    """Imita el módulo resource; failing indica qué límites rechaza el sistema."""
    RLIMIT_AS, RLIMIT_CPU = "AS", "CPU"

    def __init__(self, failing=()):
        self.failing, self.calls = set(failing), []

    def setrlimit(self, which, limits):
        self.calls.append(which)
        if which in self.failing:
            raise ValueError("current limit exceeds maximum limit")


def test_en_macos_no_limita_la_memoria_y_si_el_cpu():
    fake = FakeResource()
    assert pdf_validation.limit_resources(platform="darwin", resource=fake) == ["cpu"]
    assert fake.calls == ["CPU"]


def test_en_linux_limita_memoria_y_cpu():
    fake = FakeResource()
    assert pdf_validation.limit_resources(platform="linux", resource=fake) == ["memoria", "cpu"]


def test_si_el_sistema_rechaza_un_limite_se_aplican_los_demas():
    fake = FakeResource(failing={"AS"})
    assert pdf_validation.limit_resources(platform="linux", resource=fake) == ["cpu"]
    assert pdf_validation.limit_resources(platform="linux", resource=FakeResource(failing={"AS", "CPU"})) == []


def test_el_validador_cuenta_paginas_aunque_setrlimit_falle_como_en_macos():
    """Ejecuta el validador real en un proceso aparte con setrlimit fallando, como en una Mac."""
    script = ("import resource, runpy, sys\n"
              "def rechaza(*args):\n"
              "    raise ValueError('current limit exceeds maximum limit')\n"
              "resource.setrlimit = rechaza\n"
              f"sys.argv = [{str(VALIDATOR)!r}]\n"
              f"runpy.run_path({str(VALIDATOR)!r}, run_name='__main__')\n")
    result = subprocess.run([sys.executable, "-c", script], input=blank_pdf(3),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.strip() == b"3"


def test_el_validador_sigue_rechazando_pdf_con_javascript():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_js("app.alert('hola');")
    buffer = io.BytesIO()
    writer.write(buffer)
    result = subprocess.run([sys.executable, str(VALIDATOR)], input=buffer.getvalue(),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    assert result.returncode == 1 and result.stdout == b""