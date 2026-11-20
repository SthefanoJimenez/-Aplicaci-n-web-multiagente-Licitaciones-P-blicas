"""Demostración local de HU-010, con empresas y bases ficticias en SQLite temporales.

Ejecución desde el repositorio: .venv/Scripts/python.exe scripts/demostrar_hu010.py
No usa Neon, cuentas existentes, claves de modelos ni archivos personales.
"""
import argparse
import hashlib
import io
import os
from pathlib import Path
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    ruc_mode = parser.add_mutually_exclusive_group()
    ruc_mode.add_argument("--ruc-demo", action="store_true", help="Consulta RUC simulada, sin red.")
    ruc_mode.add_argument("--ruc-miapi", action="store_true", help="Habilita MiAPI Cloud con el token del entorno.")
    args = parser.parse_args()
    token = os.environ.get("MIAPI_RUC_TOKEN", "").strip() if args.ruc_miapi else ""
    if args.ruc_miapi and not token:
        parser.error("--ruc-miapi requiere MIAPI_RUC_TOKEN en el entorno.")
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="demostracion-hu010-") as temporary:
        directory = Path(temporary)
        os.environ.update({
            "DATABASE_URL": "sqlite:///" + (directory / "convocatorias.sqlite3").as_posix(),
            "ACCOUNTS_DATABASE_URL": "sqlite:///" + (directory / "cuentas.sqlite3").as_posix(),
            "GEMINI_API_KEY": "", "LANGSMITH_API_KEY": "", "LANGCHAIN_API_KEY": "",
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false", "SECRET_KEY": secrets.token_hex(32),
            "SMTP_HOST": "", "SMTP_USERNAME": "", "SMTP_PASSWORD": "", "SMTP_FROM": "", "PUBLIC_BASE_URL": "",
            "RESEND_API_KEY": "", "EMAIL_FROM": "", "PASSWORD_MANAGEMENT_ENABLED": "1",
            "MIAPI_RUC_TOKEN": token,
        })
        sys.path.insert(0, str(root / "app"))
        from app import app
        import auth
        from pypdf import PdfWriter
        from services import accounts, company_documents, db, eligibility, eligibility_store, ruc_lookup, tenders

        if args.ruc_demo:
            def demo_lookup(ruc):
                if ruc != "20123456789":
                    raise ruc_lookup.LookupError(404, "Sin resultados en la demostración.")
                return ruc_lookup.RucDetails(ruc, "Empresa ficticia consultada S.A.C.", "ACTIVO", "HABIDO")

            app.config.update(RUC_LOOKUP_CLIENT=demo_lookup, RUC_LOOKUP_SOURCE="la simulación local")

        engine = db.make_engine()
        db.create_tables(engine)
        factory = db.make_session_factory(engine)
        tenders.use_session_factory(factory)
        now = datetime.now(timezone.utc)
        with factory.begin() as session:
            run = db.IngestionRun(started_at=now, finished_at=now, status="ok")
            session.add(run)
            session.flush()
            for code, expected in (("9100101", "VERDE"), ("9100102", "AMARILLO"), ("9100103", "ROJO")):
                tender = db.Tender(ocid="demo-hu010-" + code, code=code,
                    title="DEMOSTRACIÓN " + expected + ": compra de acero (datos ficticios)",
                    entity="Entidad ficticia de demostración", rubros="3011", state="vigente", currency="PEN",
                    estimated_value=None if expected == "AMARILLO" else 300000,
                    published_at=now, deadline_at=now + timedelta(days=10), requirements_status="extraido",
                    first_run_id=run.id, last_run_id=run.id)
                experience = ("Experiencia mínima: facturación acumulada de S/ 500,000.00 en bienes similares. No se permite consorcio."
                              if expected == "ROJO" else "Experiencia mínima: facturación acumulada de S/ 200,000.00 en bienes similares.")
                for index, content in enumerate(("Presentar constancia de inscripción vigente en el RNP.", experience,
                                                "Personal: acreditar 2 técnicos especializados mediante sus constancias.")):
                    tender.chunks.append(db.RequirementChunk(chunk_index=index, text=content, page=index+4,
                        source="texto", file_name="bases_ficticias_"+code+".pdf", embedding=[.1]*768))
                session.add(tender)
        with app.app_context():
            private = auth.store()
            user = accounts.create_user(private, "demo-hu010@example.test", "Demo_hu010_2026!", "Empresa ficticia HU-010")
            accounts.save_profile(private, user, {"company_name":"Empresa ficticia HU-010",
                "ruc":"20123456789", "business_activity":"Venta de barras de acero y cemento", "rubros":"3011",
                "monthly_sales":Decimal("15000.50"), "experience_years":5,
                "similar_contracts_amount":Decimal("250000"), "specialized_staff":"Dos técnicos especializados"})
            for kind in ("rnp", "experiencia", "personal", "otro"):
                writer = PdfWriter()
                writer.add_blank_page(width=200, height=200)
                writer.add_metadata({"/Title":"Documento ficticio: " + kind})
                output = io.BytesIO()
                writer.write(output)
                content = output.getvalue()
                company_documents.add_document(private, user,
                    {"document_type":kind, "valid_from":(now-timedelta(days=365)).date(), "expires_on":(now+timedelta(days=365)).date()},
                    {"filename":kind+"_ficticio.pdf", "content":content, "size_bytes":len(content),
                     "page_count":1, "sha256":hashlib.sha256(content).hexdigest()})
            accounts.create_user(private, "otra-hu010@example.test", "Otra_hu010_2026!", "Otra empresa ficticia sin documentos")
            for code, tender in eligibility.load_tenders(["9100101", "9100102", "9100103"]).items():
                eligibility_store.save_inputs(private, user, code, {"regime":"32069", "whole_contract":True,
                    "guarantee_method":"retention", "rnp":"yes", "staff_count":2,
                    "reviews":{r["key"]:"own" for r in tender["requirements"]}})
        print("\nDEMOSTRACIÓN CON DATOS FICTICIOS · no es evidencia de convocatorias reales.")
        print("Abra http://127.0.0.1:5050/convocatorias")
        print("Cuenta: demo-hu010@example.test  Contraseña de demostración: Demo_hu010_2026!")
        print("Otra cuenta: otra-hu010@example.test  Contraseña de demostración: Otra_hu010_2026!")
        print("9100101: verde; 9100102: amarillo por falta del monto; 9100103: rojo por experiencia insuficiente y consorcio no admitido.")
        if args.ruc_demo:
            print("Perfil: http://127.0.0.1:5050/perfil · consulta RUC simulada: 20123456789")
        if args.ruc_miapi:
            print("Perfil: http://127.0.0.1:5050/perfil · solo Consulta RUC usa MiAPI Cloud y puede consumir cupo.")
        print("Ctrl+C termina la demostración y elimina sus datos temporales.\n", flush=True)
        try:
            app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
        finally:
            private.kw["bind"].dispose()
            engine.dispose()


if __name__ == "__main__":
    main()
