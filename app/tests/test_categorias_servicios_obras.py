"""Bienes, servicios y obras (pedido del asesor, 19/11/2026).

Cubre: la categoría en la ingesta y en la base, el listado con filtro y «Solo a las que puedo postular»,
el perfil (tipo de proveedor, categorías y registros del RNP), el personal clave, el semáforo de
servicios y obras (registro del RNP, monto, garantías y personal clave) y el asistente.
No usan internet ni Gemini.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import secrets

import pytest
from sqlalchemy import create_engine, select, text

import config
import fake_ocds
from app import app
from services import accounts, assistant, db, eligibility, guarantees, ingest, ocds_source, staff, tenders

LIMA = ocds_source.LIMA
NOW = datetime(2026, 11, 19, 8, 30, tzinfo=LIMA)
TODAY = date(2026, 11, 19)
PASSWORD = "Clave de prueba larga 2026"


# ---------------------------------------------------------------------------
# Fuente e ingesta
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [("goods", "bienes"), ("services", "servicios"), ("works", "obras"),
                                             ("consultingServices", "servicios"), ("Obras", "obras"), (None, None),
                                             ("otra cosa", None)])
def test_categoria_de_la_fuente(value, expected):
    assert ocds_source.category_of({"category": value}) == expected


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(ocds_source, "REQUEST_PAUSE_SECONDS", 0)
    with fake_ocds.FakeOCDS() as fake:
        r = fake_ocds.release
        fake.months[(2026, 11)] = [
            r(501, NOW, family="15101505"),                                   # bienes, 1510
            r(502, NOW, family="81101500", category="services"),              # servicios, 8110
            r(503, NOW, family="72141107", category="works"),                 # obras, 7214
            r(504, NOW, family="72141107", category="works", item_status="ADJUDICADO"),
        ]
        fake.months[(2026, 10)] = []
        yield fake


@pytest.fixture
def public_factory():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)
    yield factory
    tenders.use_session_factory(None)
    engine.dispose()


def run(factory, fake):
    session = ocds_source.session_for(fake.base + "/api/v1")
    return ingest.run_ingestion(factory, now=NOW, months=2, trigger="prueba", all_rubros=True,
                                fetch=lambda n, m: ocds_source.fetch_rows(n, m, session=session))


def test_la_ingesta_guarda_bienes_servicios_y_obras_con_su_categoria(public_factory, source):
    result = run(public_factory, source)
    assert result["status"] == "ok"
    assert result["by_category"] == {"bienes": 1, "servicios": 1, "obras": 1}
    with public_factory() as session:
        saved = {t.code: t.category for t in session.scalars(select(db.Tender).where(db.Tender.state == "vigente"))}
    assert saved == {"501": "bienes", "502": "servicios", "503": "obras"}


def test_con_categorias_ingesta_bienes_vuelve_al_alcance_anterior(public_factory, source, monkeypatch):
    monkeypatch.setattr(config, "CATEGORIES", ("bienes",))
    result = run(public_factory, source)
    assert result["by_category"] == {"bienes": 1}
    with public_factory() as session:
        assert [t.code for t in session.scalars(select(db.Tender))] == ["501"]


def test_una_base_anterior_recibe_la_columna_categoria_sin_perder_datos(tmp_path):
    path = tmp_path / "anterior.sqlite3"
    engine = db.make_engine(f"sqlite:///{path}")
    db.create_tables(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE convocatorias DROP COLUMN category"))
        conn.execute(text("INSERT INTO convocatorias (ocid, code, title, rubros, state, requirements_status, first_seen_at) "
                          "VALUES ('o-1', '1', 'Antigua', '1510', 'vigente', 'pendiente', '2026-11-01')"))
    engine.dispose()
    engine = db.make_engine(f"sqlite:///{path}")
    db.add_missing_columns(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)
    try:
        # Las guardadas antes del cambio eran solo bienes
        assert [t.category for t in tenders.open_tenders()] == ["bienes"]
        assert tenders.category_counts() == {"bienes": 1, "servicios": 0, "obras": 0}
    finally:
        tenders.use_session_factory(None)
        engine.dispose()


# ---------------------------------------------------------------------------
# Listado
# ---------------------------------------------------------------------------
def add_tender(factory, code, category, rubro, method, *, deadline=None, title=None, chunks=(), value=None):
    with factory.begin() as session:
        if not session.scalar(select(db.IngestionRun.id)):
            session.add(db.IngestionRun(status="ok", started_at=NOW, finished_at=NOW))
        row = db.Tender(ocid=f"o-{code}", code=code, title=title or f"CONVOCATORIA {code}", state="vigente",
                        rubros=rubro, category=category, method=method, published_at=NOW - timedelta(days=1),
                        deadline_at=deadline, estimated_value=value, currency="PEN" if value else None,
                        requirements_status="extraido" if chunks else "pendiente")
        row.documents.append(db.TenderDocument(title="Bases", url=f"https://seace.example/{code}.pdf", is_bases=True))
        for i, content in enumerate(chunks):
            row.chunks.append(db.RequirementChunk(chunk_index=i, text=content, page=10 + i, source="texto",
                                                  file_name="bases.pdf", embedding=[.1] * 768))
        session.add(row)


@pytest.fixture
def listing(public_factory):
    add_tender(public_factory, "1001", "bienes", "1510", "Subasta Inversa Electrónica")
    add_tender(public_factory, "1002", "servicios", "8110", "Concurso Público Abreviado")
    add_tender(public_factory, "1003", "obras", "7214", "Licitación Pública Abreviada")
    add_tender(public_factory, "1004", "obras", "7214", "Licitación Pública Abreviada",
               deadline=NOW - timedelta(days=2))          # vencida: rojo
    return public_factory


def test_el_listado_filtra_por_objeto_y_ofrece_los_procedimientos_de_la_base(account_client, listing):
    html = account_client.get("/convocatorias").get_data(as_text=True)
    assert "Convocatorias vigentes de bienes, servicios y obras" in html
    assert "Obras · 2 vigentes" in html and "Servicios · 1 vigente" in html
    assert '<option value="Concurso Público Abreviado"' in html
    html = account_client.get("/convocatorias?categoria=obras").get_data(as_text=True)
    assert "CONVOCATORIA 1003" in html and "CONVOCATORIA 1001" not in html
    assert 'data-category="obras"' in html


def signed_in(client, factory, profile=None):
    uid = accounts.create_user(factory, "obras@example.test", PASSWORD, "Constructora de prueba")
    accounts.save_profile(factory, uid, profile or {
        "company_name": "Constructora de prueba", "business_activity": "Ejecución de obras viales",
        "rubros": "7214", "monthly_sales": Decimal("50000"), "experience_years": 4,
        "similar_contracts_amount": Decimal("900000"), "specialized_staff": "Residente de obra",
        "supplier_type": "empresa", "categories": ["obras"], "rnp_registers": ["ejecutor_obras"]})
    with client.session_transaction() as sess:
        sess["auth_token"] = accounts.begin_session(factory, uid)
        sess["csrf_token"] = secrets.token_urlsafe(32)
    return uid


def test_para_mi_empresa_muestra_solo_lo_que_ofrece_y_ordena_por_semaforo(account_client, account_factory, listing):
    signed_in(account_client, account_factory)
    html = account_client.get("/convocatorias?para_mi=1").get_data(as_text=True)
    assert "Convocatorias para mi empresa" in html and "data-match-summary" in html
    assert "CONVOCATORIA 1001" not in html and "CONVOCATORIA 1002" not in html      # bienes y servicios: no las ofrece
    assert html.index("CONVOCATORIA 1003") < html.index("CONVOCATORIA 1004")        # amarillo antes que rojo
    assert "En 1 no cumple ni en consorcio" in html


def test_sin_sesion_no_se_ofrece_el_filtro_de_mi_empresa(account_client, listing):
    html = account_client.get("/convocatorias?para_mi=1").get_data(as_text=True)
    assert "data-for-me" not in html and "data-match-summary" not in html


# ---------------------------------------------------------------------------
# Perfil y personal clave
# ---------------------------------------------------------------------------
def test_el_perfil_guarda_tipo_de_proveedor_categorias_y_registros_del_rnp(account_client, account_factory):
    uid = signed_in(account_client, account_factory)
    response = account_client.post("/perfil", data={
        "company_name": "Constructora de prueba", "business_activity": "Obras", "rubros": "7214",
        "monthly_sales": "1000", "experience_years": "4", "similar_contracts_amount": "0",
        "specialized_staff": "Residente", "supplier_type": "persona_natural",
        "categories": ["servicios", "obras", "inventada"], "rnp_registers": ["consultor_obras", "otro"]})
    assert response.status_code == 302
    profile = accounts.get_profile(account_factory, uid)
    assert profile["supplier_type"] == "persona_natural"
    assert profile["categories"] == ["servicios", "obras"]
    assert profile["rnp_registers"] == ["consultor_obras"]
    html = account_client.get("/perfil").get_data(as_text=True)
    assert "¿Cómo postula?" in html and "Ejecutor de obras" in html and "Personal clave" in html


def test_la_migracion_agrega_las_columnas_nuevas_del_perfil(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cuentas.sqlite3'}")
    accounts.initialize_schema(engine)
    with engine.begin() as conn:
        for column in ("supplier_type", "categories", "rnp_registers"):
            conn.execute(text(f"ALTER TABLE perfiles_empresa DROP COLUMN {column}"))
    engine.dispose()
    engine = create_engine(f"sqlite:///{tmp_path / 'cuentas.sqlite3'}")
    accounts.initialize_schema(engine)
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(perfiles_empresa)"))}
    assert {"supplier_type", "categories", "rnp_registers"} <= columns
    engine.dispose()


def test_personal_clave_se_registra_y_solo_lo_borra_su_empresa(account_client, account_factory):
    uid = signed_in(account_client, account_factory)
    response = account_client.post("/perfil/personal", data={"name": "Ana Torres", "profession": "Ingeniera civil",
                                                             "years": "2", "months": "6", "licensed": "1"})
    assert response.status_code == 302
    members = staff.list_staff(account_factory, uid)
    assert members[0]["profession_key"] == "ingeniero civil" and members[0]["experience_months"] == 30
    assert account_client.post("/perfil/personal", data={"name": "X", "profession": "", "years": "a"}).status_code == 400
    other = accounts.create_user(account_factory, "otra@example.test", PASSWORD, "Otra")
    assert staff.delete_member(account_factory, other, members[0]["id"]) is False
    assert account_client.post(f"/perfil/personal/{members[0]['id']}/eliminar").status_code == 302
    assert staff.list_staff(account_factory, uid) == []


def test_lee_el_personal_clave_que_piden_las_bases():
    text_ = ("PERSONAL CLAVE. Residente de obra: Ingeniero(a) Civil colegiado y habilitado, con experiencia mínima "
             "de veinticuatro (24) meses como residente. Especialista eléctrico: Ingeniero Mecánico Electricista "
             "con 2 años en obras similares. Especialista en telecomunicaciones: Ingeniero Electrónico.")
    found = {r["profession"]: (r["months"], r["licensed"]) for r in staff.required_staff(text_)}
    assert found == {"ingeniero civil": (24, True), "ingeniero mecanico electricista": (24, False),
                     "ingeniero electronico": (None, False)}


def test_un_reglamento_tecnico_no_es_personal():
    assert staff.required_staff("Certificado de conformidad con el Reglamento Técnico del cemento") == []


# ---------------------------------------------------------------------------
# Semáforo de servicios y obras
# ---------------------------------------------------------------------------
REQ_STAFF = "Residente de obra: Ingeniero Civil colegiado y habilitado con experiencia mínima de 24 meses."


def tender_dict(category="obras", title="EJECUCION DE OBRA: MEJORAMIENTO DE PISTAS", value=None, reqs=(REQ_STAFF,)):
    return {"code": "7001", "title": title, "category": category, "method": "Licitación Pública Abreviada",
            "state": "vigente", "stage": "administrativas", "deadline_at": None, "offer_day": None,
            "published_at": NOW, "estimated_value": value, "currency": "PEN" if value else None, "items": [],
            "requirements_status": "extraido", "bases_url": None,
            "requirements": [{"text": t, "page": 20 + i, "file_name": "bases.pdf", "key": f"k{i}"} for i, t in enumerate(reqs)]}


PROFILE = {"company_name": "Constructora", "ruc": "20123456789", "business_activity": "Obras", "rubros": "7214",
           "monthly_sales": Decimal("1"), "experience_years": 4, "similar_contracts_amount": Decimal("900000"),
           "specialized_staff": "Residente", "categories": ["obras"], "rnp_registers": ["ejecutor_obras"],
           "supplier_type": "empresa"}


def member(months, licensed=True, profession="Ingeniero civil", id_=1):
    return {"id": id_, "name": "Ana", "profession": profession, "specialty": "", "licensed": licensed,
            "experience_months": months, "certifications": "", "profession_key": staff.profession_key(profession)}


def check(result, key):
    return next((c for c in result["checks"] if c["key"] == key), None)


def staff_reason(result):
    return check(result, "k0")["reason"]


def test_el_personal_clave_que_cumple_no_genera_observaciones():
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {"reviews": {"k0": "own"}}, TODAY, members=[member(30)])
    assert "ingeniero civil" not in staff_reason(result).lower()


def test_el_personal_con_menos_meses_de_los_pedidos_queda_en_amarillo():
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {"reviews": {"k0": "own"}}, TODAY, members=[member(18)])
    assert check(result, "k0")["status"] == "pending"
    assert "1 año y 6 meses" in staff_reason(result) and "2 años" in staff_reason(result)


def test_sin_colegiatura_declarada_no_cumple_el_cargo():
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {"reviews": {"k0": "own"}}, TODAY,
                                  members=[member(30, licensed=False)])
    assert "colegiado y habilitado" in staff_reason(result)


def test_sin_personal_registrado_en_obras_pide_registrarlo():
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {}, TODAY, members=[])
    assert "registre a su personal" in staff_reason(result)


def test_el_registro_del_rnp_y_la_categoria_se_contrastan_con_el_perfil():
    profile = dict(PROFILE, categories=["bienes"], rnp_registers=["bienes"])
    result = eligibility.evaluate(tender_dict(), profile, [], {}, TODAY, members=[member(30)])
    assert "ejecutor de obras" in check(result, "rnp_registro")["reason"]
    assert "convocatoria de obras" in check(result, "categoria")["reason"]
    assert result["color"] == "amarillo"
    # Con el registro y la categoría declarados no aparecen esos controles
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {}, TODAY, members=[member(30)])
    assert check(result, "rnp_registro") is None and check(result, "categoria") is None


def test_la_consultoria_de_obras_pide_el_registro_de_consultor():
    tender = tender_dict(category="servicios", title="SUPERVISION DE LA EJECUCION DE LA OBRA DE SANEAMIENTO")
    assert eligibility.contract_kind(tender) == "consultoria_obras"
    profile = dict(PROFILE, categories=["servicios"], rnp_registers=["servicios"])
    assert "consultor de obras" in check(eligibility.evaluate(tender, profile, [], {}, TODAY), "rnp_registro")["reason"]


def test_sin_valor_publicado_servicios_y_obras_no_se_estiman_con_precios_de_bienes():
    result = eligibility.evaluate(tender_dict(), PROFILE, [], {}, TODAY)
    assert result["amount"] is None
    assert "no hay precios históricos de obras" in check(result, "monto")["reason"]


POLICY = guarantees.Policy()
VALUES = {"regime": "32069", "whole_contract": True, "guarantee_method": "retention", "guarantee_capacity": 10 ** 9}


def test_en_obras_no_hay_excepcion_de_50_uit_y_la_retencion_llega_a_cinco_millones():
    small = guarantees.calculate(Decimal("200000"), VALUES, TODAY, POLICY, kind="obras")
    assert small["exempt"] is False and small["amount"] == Decimal("20000.00")
    assert "no se aplica a obras" in small["reason"]
    same_in_goods = guarantees.calculate(Decimal("200000"), VALUES, TODAY, POLICY, kind="bienes")
    assert same_in_goods["exempt"] is True                     # 200 000 ≤ 50 UIT (275 000)
    works = guarantees.calculate(Decimal("4000000"), VALUES, TODAY, POLICY, kind="obras")
    goods = guarantees.calculate(Decimal("4000000"), VALUES, TODAY, POLICY, kind="bienes")
    assert works["retention_allowed"] is True and goods["retention_allowed"] is False
    assert guarantees.calculate(Decimal("6000000"), VALUES, TODAY, POLICY, kind="obras")["retention_allowed"] is False
    mype = guarantees.calculate(Decimal("6000000"), dict(VALUES, mype=True), TODAY, POLICY, kind="obras")
    assert mype["retention_allowed"] is True


# ---------------------------------------------------------------------------
# Asistente
# ---------------------------------------------------------------------------
def ask(factory, uid, query):
    return assistant.ask_assistant(query, uid, factory, email="obras@example.test", today=TODAY,
                                   call_model=lambda prompt: None)


def test_el_asistente_busca_por_la_categoria_que_pide_la_consulta(account_factory, listing):
    uid = accounts.create_user(account_factory, "obras@example.test", PASSWORD, "Constructora")
    accounts.save_profile(account_factory, uid, dict(PROFILE, rubros="", business_activity="Constructora", categories=[]))
    result = ask(account_factory, uid, "Ejecuto obras, ¿a cuáles puedo postular?")
    assert "1003" in result["response"] and "1001" not in result["response"] and "1002" not in result["response"]
    assert "Proceso 1004" not in result["response"]          # su plazo ya venció: no se propone


def test_el_asistente_pregunta_como_postula_si_el_perfil_no_lo_dice(account_factory, listing):
    uid = accounts.create_user(account_factory, "obras@example.test", PASSWORD, "Constructora")
    accounts.save_profile(account_factory, uid, dict(PROFILE, supplier_type="", categories=["obras"]))
    result = ask(account_factory, uid, "¿A qué puedo postular?")
    assert "persona natural" in result["response"] and "**empresa**" in result["response"]


@pytest.mark.parametrize("value, expected", [(None, True), ("", True), ("todos", True), ("TODOS", True),
                                             ("solicitados", False), ("piloto", False)])
def test_por_defecto_la_ingesta_guarda_todos_los_rubros(monkeypatch, value, expected):
    """20/11/2026: sin RUBROS_INGESTA se guardan todos los rubros de bienes, servicios y obras."""
    import importlib
    if value is None:
        monkeypatch.delenv("RUBROS_INGESTA", raising=False)
    else:
        monkeypatch.setenv("RUBROS_INGESTA", value)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False, raising=False)
    try:
        assert importlib.reload(config).ALL_RUBROS is expected
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_el_filtro_de_rubros_va_de_mas_a_menos_vigentes_y_se_acota_por_objeto(account_client, listing):
    """20/11/2026: los piloto ya no van primero; con un objeto elegido solo aparecen sus rubros."""
    html = account_client.get("/convocatorias").get_data(as_text=True)
    positions = [html.index(f'<option value="{code}"') for code in ("7214", "1510", "8110", "2510")]
    assert positions == sorted(positions)          # 7214 (2 vigentes), 1510 y 8110 (1), 2510 piloto sin datos
    assert "data-category-filter" in html          # al cambiar el objeto se recarga el filtro de rubros
    html = account_client.get("/convocatorias?categoria=servicios").get_data(as_text=True)
    assert '<option value="8110"' in html and '<option value="7214"' not in html and '<option value="1510"' not in html
    assert "Todos los rubros de servicios" in html


def test_el_catalogo_de_rubros_se_filtra_por_objeto(account_client, listing):
    html = account_client.get("/rubros").get_data(as_text=True)
    assert 'name="categoria"' in html and "bienes, servicios y obras" in html
    html = account_client.get("/rubros?categoria=obras").get_data(as_text=True)
    assert '<span class="chip">7214</span>' in html and '<span class="chip">8110</span>' not in html
    assert "con vigentes de obras" in html and "categoria=obras" in html     # «Ver convocatorias» mantiene el objeto
    html = account_client.get("/rubros?categoria=inventada").get_data(as_text=True)
    assert '<span class="chip">8110</span>' in html                         # un objeto inválido no filtra