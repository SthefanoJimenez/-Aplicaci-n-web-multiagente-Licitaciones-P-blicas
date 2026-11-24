"""Capacitación del personal clave (24/11/2026).

Medición en Neon del 24/11/2026: 25 de 58 bases de servicios leídas mencionan capacitación (apartado «Capacitación
del personal clave» de las bases estándar), contra 16 de 522 en bienes y 3 de 59 en obras. Gemini lee el tema y
las horas por cargo; la empresa escribe el tema y las horas al subir cada certificado; las horas se comparan
directo y el tema solo da verde si coincide claramente.
"""
import io
from datetime import datetime, timezone

from services import bases_facts, db, eligibility, rag, staff
from test_categorias_servicios_obras import PROFILE, TODAY, member
from test_personal_fichas import add_person, text_pdf, upload
from test_requisitos_analizados import factory, with_facts  # noqa: F401

SECTION = """[página 40]
C.2.2 CAPACITACIÓN DEL PERSONAL CLAVE
Requisitos: Supervisor de seguridad: Ingeniero Civil con ciento veinte (120) horas lectivas de capacitación en
Seguridad y Salud en el Trabajo."""


def raw(hours=120, kind="personal"):
    return {"requisitos": [{"tipo": kind, "descripcion": "Supervisor de seguridad", "cargo": "Supervisor de seguridad",
                            "profesion": "Ingeniero Civil", "capacitacion_tema": "Seguridad y Salud en el Trabajo",
                            "capacitacion_horas": hours, "pagina": 40, "cita": "120 horas lectivas"}]}


def test_el_analisis_guarda_tema_y_horas_solo_si_estan_escritos():
    item = bases_facts.clean(raw(), SECTION)["requisitos"][0]
    assert item["capacitacion_tema"] == "Seguridad y Salud en el Trabajo" and item["capacitacion_horas"] == 120
    assert bases_facts.clean(raw(hours=200), SECTION)["requisitos"][0]["capacitacion_horas"] is None
    other = bases_facts.clean(raw(kind="otro"), SECTION)["requisitos"][0]
    assert other["capacitacion_tema"] is None and other["capacitacion_horas"] is None
    prompt = bases_facts.build_prompt(SECTION, "", "servicios")
    assert "capacitacion_horas" in prompt and "factor de evaluación" in prompt


def person(*trainings):
    evidence = {"kinds": ["cv", "colegiatura"] + (["capacitacion"] if trainings else []), "mismatch": [], "expired": [],
                "trainings": [{"topic": topic, "hours": hours, "filename": "c.pdf"} for topic, hours in trainings]}
    return [dict(member(60), evidence=evidence)]


def staff_check(members, **fact):
    facts = {"valor_estimado": {}, "consorcio": {"permitido": True},
             "requisitos": [dict(raw()["requisitos"][0], colegiado=True, experiencia_meses=24, **fact)]}
    result = eligibility.evaluate(with_facts(facts, category="servicios"), PROFILE, [], {}, TODAY, members=members)
    check = next(c for c in result["checks"] if c["label"] == "Supervisor de seguridad")
    return check["status"], check["reason"]


def test_las_horas_de_certificados_del_mismo_tema_se_suman():
    # «SST» es la sigla de Seguridad y Salud en el Trabajo
    state, reason = staff_check(person(("Curso de SST", 80), ("Seguridad y salud ocupacional", 60)))
    assert state == "pass" and "suma 140 horas" in reason and "piden 120" in reason


def test_si_no_alcanzan_las_horas_dice_cuantas_tiene():
    state, reason = staff_check(person(("Seguridad y Salud en el Trabajo", 80)))
    assert state == "pending" and "suma 80 de las 120 horas" in reason


def test_otro_tema_queda_por_revisar_con_ambos_textos():
    state, reason = staff_check(person(("Excel avanzado", 200)))
    assert state == "pending" and "«Excel avanzado» (200 h)" in reason and "revise si sirven" in reason


def test_sin_certificado_pide_subirlo_y_para_el_contrato_solo_avisa():
    state, reason = staff_check(person())
    assert state == "pending" and "suba en Personal clave el certificado de capacitación de Ana" in reason
    state, reason = staff_check(person(), momento="contrato")
    assert state == "assumed" and "firmar el contrato" in reason


def test_sin_capacitacion_en_las_bases_no_cambia_nada():
    state, reason = staff_check(person(), capacitacion_tema=None, capacitacion_horas=None)
    assert state == "pass" and "capacitación" not in reason


def test_al_subir_un_certificado_se_piden_tema_y_horas(account_client, account_factory):
    uid, member_id = add_person(account_client, account_factory)
    cert = text_pdf(["CERTIFICADO", "Otorgado a JUAN PEREZ por aprobar el curso de Seguridad y Salud en el Trabajo",
                     "con una duracion de 120 horas lectivas."])
    upload(account_client, member_id, cert, "capacitacion", "sst.pdf")
    assert staff.list_staff(account_factory, uid)[0]["documents"] == []           # sin tema no se guarda
    page = account_client.get("/perfil/personal").get_data(as_text=True)
    assert "Escriba el tema de la capacitación" in page and "Tema de la capacitación" in page
    upload(account_client, member_id, cert, "capacitacion", "sst.pdf", training_topic="Seguridad y salud en el trabajo")
    doc = staff.list_staff(account_factory, uid)[0]["documents"][0]
    assert doc["training_hours"] == 120 and "Horas tomadas del certificado: 120" in doc["read_detail"]
    person_ = staff.list_staff(account_factory, uid)[0]
    assert person_["evidence"]["trainings"][0]["hours"] == 120
    assert "Seguridad y salud en el trabajo · 120 horas" in account_client.get("/perfil/personal").get_data(as_text=True)
    other = text_pdf(["CERTIFICADO", "Curso de primeros auxilios", "duracion: 40 horas"])
    upload(account_client, member_id, other, "capacitacion", "aux.pdf", training_topic="Primeros auxilios",
           training_hours="60")
    doc = next(d for d in staff.list_staff(account_factory, uid)[0]["documents"] if d["filename"] == "aux.pdf")
    assert doc["read_status"] == "no_coincide" and "Declaró 60 horas; el certificado dice 40" in doc["read_detail"]


def test_solo_se_vuelven_a_analizar_las_bases_de_servicios_que_mencionan_capacitacion(factory):  # noqa: F811
    def add(code, category="servicios", text="C.2.2 CAPACITACIÓN DEL PERSONAL CLAVE 120 horas", version=1):
        with factory.begin() as session:
            row = db.Tender(ocid=f"o-{code}", code=code, title="X", state="vigente", category=category,
                            requirements_status="extraido", bases_facts={"version": version, "requisitos": []},
                            bases_facts_at=datetime.now(timezone.utc))
            row.chunks.append(db.RequirementChunk(chunk_index=0, text=text, page=40, source="texto",
                                                  file_name="bases.pdf", embedding=[.1] * 768))
            session.add(row)
    add("1"), add("2", version=2), add("3", category="bienes"), add("4", text="Experiencia S/ 100,000.00")
    with factory() as session:
        assert rag.training_codes(session) == ["1"]


def test_basta_con_una_de_las_alternativas_que_dan_las_bases():
    """Temas reales del 24/11/2026 (bases 1268333 y 1267322): vienen como alternativas con «y/o» y «o»."""
    music = ("educación musical y/o pedagogía musical y/o interpretación o ejecución instrumental y/o dirección de "
             "bandas musicales y/o dirección de ensambles musicales")
    works = ("Gestión de Proyectos de Ingeniería y/o Residencia y/o Supervisión de Proyectos de Ingeniería y/o "
             "Residencia o Supervisión de Obras")
    assert staff_check(person(("Pedagogía musical", 20)), capacitacion_tema=music, capacitacion_horas=16)[0] == "pass"
    assert staff_check(person(("Supervisión de obras viales", 90)), capacitacion_tema=works, capacitacion_horas=80)[0] == "pass"
    state, reason = staff_check(person(("Excel avanzado", 90)), capacitacion_tema=works, capacitacion_horas=80)
    assert state == "pending" and "revise si sirven" in reason
    reason = staff_check(person(("Excel avanzado", 90)), capacitacion_tema=music, capacitacion_horas=16)[1]
    assert "…»" in reason and len(music) > 140                                       # el tema largo se acorta
    # Con «y» (sin «o») se compara el tema completo: basta con compartir la mitad de sus palabras
    both = "Buenas Prácticas de Distribución y Buenas Prácticas de Almacenamiento en Droguerías"
    assert staff_check(person(("Buenas prácticas de almacenamiento en droguerías", 8)), capacitacion_tema=both,
                       capacitacion_horas=8)[0] == "pass"