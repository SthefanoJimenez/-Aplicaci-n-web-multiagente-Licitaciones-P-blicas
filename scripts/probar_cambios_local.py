"""Ejecuta la suite con variables locales aisladas; no carga credenciales del .env."""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="licitafacil-pruebas-") as folder:
        directory = Path(folder)
        environment = {**os.environ,
            "DATABASE_URL": "sqlite:///" + (directory / "licitaciones.sqlite3").as_posix(),
            "ACCOUNTS_DATABASE_URL": "sqlite:///" + (directory / "cuentas.sqlite3").as_posix(),
            "SECRET_KEY": secrets.token_hex(32), "GEMINI_API_KEY": "", "LANGSMITH_API_KEY": "",
            "LANGCHAIN_API_KEY": "", "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
            "SMTP_HOST": "", "SMTP_USERNAME": "", "SMTP_PASSWORD": "", "SMTP_FROM": "", "PUBLIC_BASE_URL": "",
            "RESEND_API_KEY": "", "EMAIL_FROM": "", "EMAIL_TRANSPORT": "smtp", "MIAPI_RUC_TOKEN": ""}
        # La copia de pruebas local no necesita las cookies Secure del entorno Render.
        environment.pop("RENDER", None)
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]],
                                cwd=root, env=environment)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
