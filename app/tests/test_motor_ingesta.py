
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config
import fake_ocds
from services import db, ingest, ocds_source

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 6, 8, 30, tzinfo=ocds_source.LIMA)


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(ocds_source, "REQUEST_PAUSE_SECONDS", 0)
    with fake_ocds.FakeOCDS() as fake:
        rows = [fake_ocds.release(n, NOW) for n in (401, 402, 403, 404)]
        for row, kind in zip(rows, ("pdf", "zip", "rar", "docx")):
            row["tender"]["documents"][0]["format"] = kind
        fake.months[(2026, 11)] = rows
        fake.months[(2026, 10)] = []
        yield fake


@pytest.fixture
def job(tmp_path, monkeypatch, source):
    """ingest_job con una base SQLite propia y la API simulada."""
    import ingest_job
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'base.db'}")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("INGEST_TRIGGER", raising=False)
    real_run = ingest.run_ingestion
    session = ocds_source.session_for(source.base + "/api/v1")
    monkeypatch.setattr(ingest, "run_ingestion", lambda factory, trigger=None: real_run(
        factory, now=None, fetch=lambda n, m: ocds_source.fetch_rows(NOW, m, session=session), months=2,
        trigger=trigger))
    return ingest_job


def runs(tmp_path):
    engine = db.make_engine(f"sqlite:///{tmp_path / 'base.db'}")
    with db.make_session_factory(engine)() as session:
        result = [(r.trigger, r.status) for r in session.query(db.IngestionRun).order_by(db.IngestionRun.id)]
    engine.dispose()
    return result


# ---------------------------------------------------------------------------
# Cada ejecución queda registrada con su origen
# ---------------------------------------------------------------------------
def test_origin_comes_from_the_scheduled_task_then_github_then_local(monkeypatch):
    import ingest_job
    monkeypatch.delenv("INGEST_TRIGGER", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    assert ingest_job.run_origin() == "local"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    assert ingest_job.run_origin() == "workflow_dispatch"
    monkeypatch.setenv("INGEST_TRIGGER", "programada_imac")
    assert ingest_job.run_origin() == "programada_imac"


def test_scheduled_run_is_recorded_in_the_database_with_its_origin(job, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INGEST_TRIGGER", "programada_imac")
    assert job.main_with(["--sin-rag"]) == 0
    assert "origen: programada_imac" in capsys.readouterr().out
    monkeypatch.delenv("INGEST_TRIGGER")
    assert job.main_with(["--sin-rag"]) == 0
    assert runs(tmp_path) == [("programada_imac", "ok"), ("local", "ok")]


def test_runs_command_lists_the_runs_and_the_days_the_task_ran(job, tmp_path, monkeypatch, capsys):
    assert job.main_with(["--ejecuciones"]) == 1
    assert "Todavía no hay ejecuciones" in capsys.readouterr().out
    monkeypatch.setenv("INGEST_TRIGGER", "programada_imac")
    job.main_with(["--sin-rag"])
    capsys.readouterr()
    assert job.main_with(["--ejecuciones", "5"]) == 0
    out = capsys.readouterr().out
    assert "Últimas 1 ejecuciones (hora de Lima):" in out
    assert "· programada_imac · ok ·" in out and "4 vigentes" in out
    assert "Tarea programada (programada_imac): 1 de los últimos 7 días con ejecución correcta" in out


def test_a_failed_run_is_also_recorded(job, tmp_path, monkeypatch, capsys, source):
    monkeypatch.setenv("INGEST_TRIGGER", "programada_imac")
    source.fail = 500
    assert job.main_with(["--sin-rag"]) == 1
    assert runs(tmp_path) == [("programada_imac", "fallida")]
    job.main_with(["--ejecuciones"])
    assert "· programada_imac · fallida" in capsys.readouterr().out


def test_bases_sample_gives_one_open_tender_per_declared_format(job, capsys):
    job.main_with(["--sin-rag"])
    capsys.readouterr()
    assert job.main_with(["--muestra-bases"]) == 0
    out = capsys.readouterr().out
    for label, code in (("PDF", "401"), ("ZIP", "402"), ("RAR", "403"), ("Word", "404")):
        assert f"- {label}: {code} · " in out and f"/convocatorias/{code}/bases" in out


# ---------------------------------------------------------------------------
# GitHub Actions: solo a mano
# ---------------------------------------------------------------------------
def test_ingestion_workflow_runs_only_by_hand():
    text = (REPO / ".github/workflows/ingesta_diaria.yml").read_text(encoding="utf-8")
    lines = [line.split("#")[0].rstrip() for line in text.splitlines()]      # sin los comentarios
    start = lines.index("on:")
    block = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break                                              # terminó el bloque "on:"
        if line.strip():
            block.append(line.strip())
    assert block == ["workflow_dispatch:"]                     # sin "schedule" ni "cron"
    assert "cron:" not in "\n".join(lines)


# ---------------------------------------------------------------------------
# Tarea programada de la iMac (scripts/imac/)
# ---------------------------------------------------------------------------
SCRIPTS = REPO / "scripts/imac"
# Los scripts son de macOS. En Windows se omiten: el "bash" que se encuentre puede ser el de WSL, que no
# entiende las rutas de Windows, y los scripts falsos del Python y de caffeinate no se pueden ejecutar.
ON_UNIX = sys.platform in ("darwin", "linux") and shutil.which("bash") is not None


@pytest.mark.skipif(not ON_UNIX, reason="scripts de macOS: solo en macOS o Linux")
def test_scheduler_scripts_have_valid_syntax():
    for name in ("ingesta_diaria.sh", "instalar.sh", "desinstalar.sh"):
        assert subprocess.run(["bash", "-n", str(SCRIPTS / name)]).returncode == 0, name


@pytest.mark.skipif(not ON_UNIX, reason="scripts de macOS: solo en macOS o Linux")
def test_daily_script_runs_the_job_from_app_with_its_origin_and_one_at_a_time(tmp_path):
    """Copia el script a un repositorio de prueba con un Python falso, para no tocar la base real."""
    repo = tmp_path / "repo"
    (repo / "scripts/imac").mkdir(parents=True)
    (repo / "app").mkdir()
    shutil.copy(SCRIPTS / "ingesta_diaria.sh", repo / "scripts/imac/")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "caffeinate").write_text('#!/bin/bash\nshift\n"$@"\n')
    (bin_dir / "pyfake").write_text('#!/bin/bash\necho "job $* origen=$INGEST_TRIGGER en $(basename "$PWD")"\n'
                                    'sleep 1\nexit 0\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", LICITAFACIL_PYTHON=str(bin_dir / "pyfake"))
    env.pop("INGEST_TRIGGER", None)
    first = subprocess.Popen(["bash", str(repo / "scripts/imac/ingesta_diaria.sh")], env=env)
    import time
    time.sleep(0.4)
    second = subprocess.run(["bash", str(repo / "scripts/imac/ingesta_diaria.sh")], env=env)
    assert first.wait(timeout=10) == 0 and second.returncode == 0
    log = next((repo / "app/logs").glob("ingesta_*.log")).read_text()
    assert "job ingest_job.py origen=programada_imac en app" in log
    assert "Ya hay una ingesta en curso; esta se omite." in log       # la segunda no se superpuso
    assert "· fin · código 0" in log
    assert not (repo / "app/logs/.ingesta.lock").exists()             # el candado se libera al terminar


def test_installer_records_python_path_and_daily_time():
    text = (SCRIPTS / "instalar.sh").read_text(encoding="utf-8")
    assert "StartCalendarInterval" in text and "LICITAFACIL_PYTHON" in text and "programada_imac" in text
    assert 'HOUR="${1:-08}"' in text and 'MINUTE="${2:-30}"' in text     # 08:30 de Lima, como el horario anterior


# ---------------------------------------------------------------------------
# Tarea programada de Windows (scripts/windows/)
# ---------------------------------------------------------------------------
WINDOWS = REPO / "scripts/windows"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def test_windows_scripts_are_plain_ascii_and_define_the_task():
    """Windows PowerShell 5.1 lee como ANSI los .ps1 sin BOM: con tildes, el registro salía "cÃ³digo"
    (07/11/2026, al copiar los scripts a mano en VS Code). En ASCII da igual cómo se guarden."""
    for name in ("ingesta_diaria.ps1", "instalar.ps1", "desinstalar.ps1"):
        data = (WINDOWS / name).read_bytes()
        data = data[3:] if data[:3] == b"\xef\xbb\xbf" else data
        assert all(byte < 128 for byte in data), f"{name} tiene caracteres que no son ASCII"
    installer = (WINDOWS / "instalar.ps1").read_text(encoding="utf-8-sig")
    assert 'param([string]$Hora = "08:30")' in installer and "Register-ScheduledTask" in installer
    assert "-StartWhenAvailable" in installer and "-MultipleInstances IgnoreNew" in installer
    daily = (WINDOWS / "ingesta_diaria.ps1").read_text(encoding="utf-8-sig")
    assert '[string]$Origen = "programada_windows"' in daily and "&&" not in daily


@pytest.mark.skipif(POWERSHELL is None, reason="sin PowerShell")
def test_windows_scripts_parse_without_errors():
    for name in ("ingesta_diaria.ps1", "instalar.ps1", "desinstalar.ps1"):
        check = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                 f"'{WINDOWS / name}', [ref]$null, [ref]$e); if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", check], capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stdout)


@pytest.mark.skipif(POWERSHELL is None, reason="sin PowerShell")
def test_windows_daily_script_runs_the_job_with_its_origin_and_one_at_a_time(tmp_path):
    """Copia el script a un repositorio de prueba (con espacios en la ruta) y usa un Python falso."""
    repo = tmp_path / "repo con espacios"
    (repo / "scripts/windows").mkdir(parents=True)
    (repo / "app").mkdir()
    shutil.copy(WINDOWS / "ingesta_diaria.ps1", repo / "scripts/windows/")
    if sys.platform == "win32":
        fake = tmp_path / "pyfake.cmd"
        fake.write_text("@echo off\r\necho job %* origen=%INGEST_TRIGGER%\r\nping -n 3 127.0.0.1 >nul\r\nexit /b 0\r\n")
    else:
        fake = tmp_path / "pyfake"
        fake.write_text('#!/bin/bash\necho "job $* origen=$INGEST_TRIGGER"\nsleep 2\nexit 0\n')
        fake.chmod(0o755)
    command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(repo / "scripts/windows/ingesta_diaria.ps1"), "-Python", str(fake)]
    first = subprocess.Popen(command)
    import time
    log_dir = repo / "app/logs"
    for _ in range(100):                                   # espera a que la primera tome el candado
        if (log_dir / "ingesta.lock").exists():
            break
        time.sleep(0.1)
    second = subprocess.run(command)
    assert first.wait(timeout=60) == 0 and second.returncode == 0
    log = next(log_dir.glob("ingesta_*.log")).read_text(encoding="utf-8-sig")
    assert "job ingest_job.py origen=programada_windows" in log
    assert "Ya hay una ingesta en curso; esta se omite." in log
    assert "| fin | codigo 0" in log and "Ã" not in log
    assert not (log_dir / "ingesta.lock").exists()