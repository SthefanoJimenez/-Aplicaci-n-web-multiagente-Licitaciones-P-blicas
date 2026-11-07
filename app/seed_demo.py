"""Script de inicialización y siembra de base de datos de desarrollo local (dev_tenders.db).

Crea las tablas en SQLite y siembra convocatorias de demostración con fragmentos de bases
para que tanto la pantalla de /convocatorias como el /asistente funcionen de inmediato en local.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Agregar app/ al path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from services import db

NOW = datetime.now(timezone.utc)


def seed():
    data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "dev_tenders.db"
    db_url = f"sqlite:///{db_path}"

    print(f"Inicializando base SQLite local en: {db_path}")
    engine = db.make_engine(db_url)
    db.create_tables(engine)
    factory = db.make_session_factory(engine)

    with factory.begin() as session:
        # 1. Registro de actualización exitosa
        run = db.IngestionRun(
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=1),
            status="ok",
            months=f"{NOW.year}-{NOW.month:02d}",
            source_open_goods=8,
            source_open_pilot=4,
            saved_open=4,
        )
        session.add(run)
        session.flush()

        # Limpiar convocatorias anteriores si existieran
        session.query(db.RequirementChunk).delete()
        session.query(db.TenderDocument).delete()
        session.query(db.Tender).delete()

        # Convocatoria 1: Combustibles en La Libertad (exige OSINERGMIN y S/ 120,000)
        t1 = db.Tender(
            ocid="ocds-dgv273-seacev3-1001",
            code="1001",
            title="ADQUISICION DE DIESEL B5 S-50 PARA UNIDADES Y MAQUINARIA",
            entity="MUNICIPALIDAD PROVINCIAL DE TRUJILLO",
            department="LA LIBERTAD",
            method="Subasta Inversa Electrónica",
            rubros="1510",
            state="vigente",
            published_at=NOW - timedelta(days=2),
            estimated_value=125000.50,
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        t1.documents.append(db.TenderDocument(
            title="Bases Administrativas Integradas",
            url="https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=1001_bases",
            is_bases=True,
        ))
        t1.chunks.append(db.RequirementChunk(
            chunk_index=0,
            page=4,
            text="Capítulo III - Requisitos de Calificación: El postor debe acreditar inscripción vigente en el Registro de Hidrocarburos de OSINERGMIN. Experiencia del postor: facturación acumulada de al menos S/ 120,000.00 en venta de combustibles líquidos.",
            source="pdf",
            file_name="bases_1001.pdf",
            embedding=[0.1] * 768,
        ))
        session.add(t1)

        # Convocatoria 2: Vehículos en Lima (Licitación Pública, exige S/ 250,000)
        t2 = db.Tender(
            ocid="ocds-dgv273-seacev3-1002",
            code="1002",
            title="ADQUISICION DE CAMIONETAS PICK UP 4X4 DOBLE CABINA",
            entity="MINISTERIO DEL INTERIOR",
            department="LIMA",
            method="Licitación Pública",
            rubros="2510",
            state="vigente",
            published_at=NOW - timedelta(days=3),
            estimated_value=320000.00,
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        t2.documents.append(db.TenderDocument(
            title="Bases del Procedimiento",
            url="https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=1002_bases",
            is_bases=True,
        ))
        t2.chunks.append(db.RequirementChunk(
            chunk_index=0,
            page=7,
            text="Requisitos de Calificación - Experiencia: facturación acumulada equivalente a S/ 250,000.00 por la venta de vehículos iguales o similares durante los ocho años anteriores. Autorización de fábrica o distribuidor de la marca en el Perú.",
            source="pdf",
            file_name="bases_1002.pdf",
            embedding=[0.2] * 768,
        ))
        session.add(t2)

        # Convocatoria 3: Materiales de Construcción en Arequipa (Adjudicación Simplificada)
        t3 = db.Tender(
            ocid="ocds-dgv273-seacev3-1003",
            code="1003",
            title="ADQUISICION DE CEMENTO PORTLAND TIPO I Y AGREGADOS PARA OBRA",
            entity="GOBIERNO REGIONAL DE AREQUIPA",
            department="AREQUIPA",
            method="Adjudicación Simplificada",
            rubros="3011",
            state="vigente",
            published_at=NOW - timedelta(days=1),
            estimated_value=95000.00,
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        t3.documents.append(db.TenderDocument(
            title="Bases Administrativas",
            url="https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=1003_bases",
            is_bases=True,
        ))
        t3.chunks.append(db.RequirementChunk(
            chunk_index=0,
            page=5,
            text="Capítulo III: Experiencia en la especialidad por un monto mínimo acumulado de S/ 60,000.00 en venta de cemento y materiales de construcción. Cumplimiento de norma técnica NTP 334.009.",
            source="pdf",
            file_name="bases_1003.pdf",
            embedding=[0.3] * 768,
        ))
        session.add(t3)

        # Convocatoria 4: Subasta Inversa de Combustibles (Sin exigencia de experiencia)
        t4 = db.Tender(
            ocid="ocds-dgv273-seacev3-1004",
            code="1004",
            title="SUMINISTRO DE GASOHOL REGULAR Y PREMIUM",
            entity="MUNICIPALIDAD DISTRITAL DE VICTOR LARCO HERRERA",
            department="LA LIBERTAD",
            method="Subasta Inversa Electrónica",
            rubros="1510",
            state="vigente",
            published_at=NOW - timedelta(hours=12),
            estimated_value=65000.00,
            currency="PEN",
            first_run_id=run.id,
            last_run_id=run.id,
        )
        t4.documents.append(db.TenderDocument(
            title="Bases de Subasta Inversa",
            url="https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode=1004_bases",
            is_bases=True,
        ))
        t4.chunks.append(db.RequirementChunk(
            chunk_index=0,
            page=3,
            text="Subasta Inversa Electrónica: No se evalúa experiencia en la especialidad por tratarse de un bien común con Ficha Técnica aprobada. Solo se requiere inscripción en el Registro de Hidrocarburos de OSINERGMIN y RNP vigente.",
            source="pdf",
            file_name="bases_1004.pdf",
            embedding=[0.1] * 768,
        ))
        session.add(t4)

    # Actualizar o crear app/.env si no existe DATABASE_URL
    env_file = Path(__file__).resolve().parent / ".env"
    env_content = f"DATABASE_URL={db_url}\n"
    if not env_file.exists():
        env_file.write_text(env_content, encoding="utf-8")
        print(f"Creado archivo .env con DATABASE_URL={db_url}")
    else:
        current_env = env_file.read_text(encoding="utf-8")
        if "DATABASE_URL" not in current_env:
            with open(env_file, "a", encoding="utf-8") as f:
                f.write(f"\n{env_content}")
            print(f"Agregado DATABASE_URL={db_url} a .env")

    engine.dispose()
    print("[OK] Base de datos local sembrada exitosamente con 4 convocatorias y fragmentos de bases.")


if __name__ == "__main__":
    seed()
