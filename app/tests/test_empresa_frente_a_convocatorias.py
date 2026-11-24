"""Revisión del 23/11/2026: que los datos de la empresa sirvan de verdad para evaluar las convocatorias.

Casos encontrados al probar de punta a punta con una empresa, su personal, sus documentos y un consorcio:
- las bases fijan un monto de experiencia menor para MYPE (base real 1250704: S/ 1,400,000 y S/ 320,000);
- lo que la empresa no alcanza sola, ¿lo alcanza con su consorcio? (Consorcios, HU-012);
- el consorcio toma de las bases analizadas el monto, los años y el personal que piden.
"""
from decimal import Decimal
from pathlib import Path

from services import bases_facts, consortia, eligibility
from test_categorias_servicios_obras import PROFILE, TODAY, member
from test_requisitos_analizados import with_facts

REAL = (Path(__file__).resolve().parents[2] / "analisis/salidas/sp006/secciones/1250704.txt")


def experience(**extra):
    item = {"tipo": "experiencia", "descripcion": "Facturación en combustibles", "monto_minimo": 1400000.0,
            "moneda": "PEN", "anios": 10, "pagina": 22, "cita": "S/ 1,400,000.00"}
    return {"valor_estimado": {}, "consorcio": {"permitido": True}, "requisitos": [dict(item, **extra)]}


def label_status(result, label):
    return next((c["status"], c["reason"]) for c in result["checks"] if c.get("label") == label)


def test_lee_el_monto_para_mype_de_una_base_real():
    text = REAL.read_text(encoding="utf-8")
    assert bases_facts.mype_amount(text, 1400000) == 320000.0
    assert bases_facts.mype_amount(text, 300000) is None                 # nunca uno mayor que el general
    cleaned = bases_facts.clean({"requisitos": [{"tipo": "experiencia", "monto_minimo": 1400000}]}, text)
    assert cleaned["requisitos"][0]["monto_minimo_mype"] == 320000.0


def test_una_mype_se_compara_con_el_monto_para_mype():
    facts = experience(monto_minimo_mype=320000.0)
    mype = eligibility.evaluate(with_facts(facts), dict(PROFILE, is_mype=True), [], {}, TODAY, members=[])
    status, reason = label_status(mype, "Facturación en combustibles")
    assert status == "pass" and "S/ 320,000.00 (monto para MYPE)" in reason
    big = eligibility.evaluate(with_facts(facts), dict(PROFILE, is_mype=False), [], {}, TODAY, members=[])
    assert label_status(big, "Facturación en combustibles")[0] == "short"


def test_un_analisis_anterior_sin_el_monto_para_mype_lo_toma_del_texto_leido():
    tender = dict(with_facts({"requisitos": []}), requirements=[
        {"text": REAL.read_text(encoding="utf-8"), "page": 21, "file_name": "b.pdf", "key": "k"}])
    tender = eligibility.use_facts(tender, experience())
    result = eligibility.evaluate(tender, dict(PROFILE, is_mype=True), [], {}, TODAY, members=[])
    assert label_status(result, "Facturación en combustibles")[0] == "pass"


def group(name="Consorcio Norte Sur", partner_amount="600000", partner_years=9, partner_staff=()):
    own = consortia.Member(company_id=1, name="Constructora", role="lider",
                           similar_contracts_amount=Decimal("900000"), experience_years=4)
    partner = consortia.Member(company_id=2, name="Petro Sur SAC", similar_contracts_amount=Decimal(partner_amount),
                               experience_years=partner_years)
    return {"id": 1, "name": name, "company_id": 1, "members": [own, partner],
            "staff": [dict(p, company_id=2, company_name="Petro Sur SAC") for p in partner_staff]}


def test_lo_que_no_alcanza_sola_lo_alcanza_con_su_consorcio():
    facts = experience()
    result = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[], consortia=[group()])
    status, reason = label_status(result, "Facturación en combustibles")
    assert status == "consortium" and "suman S/ 1,500,000.00" in reason and "Petro Sur SAC" in reason
    weak = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[],
                                consortia=[group(partner_amount="100000")])
    status, reason = label_status(weak, "Facturación en combustibles")
    assert status == "short" and "Ninguno de sus consorcios alcanza" in reason
    alone = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[])
    assert "puede formarlo en Consorcios" in label_status(alone, "Facturación en combustibles")[1]


def test_si_las_bases_no_admiten_consorcio_no_cuenta():
    facts = dict(experience(), consorcio={"permitido": False})
    result = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[], consortia=[group()])
    assert label_status(result, "Facturación en combustibles")[0] == "fail"


def test_personal_que_aporta_una_socia_del_consorcio():
    facts = {"requisitos": [{"tipo": "personal", "descripcion": "Especialista ambiental", "profesion": "Ingeniero Ambiental",
                             "experiencia_meses": 36, "cita": "Especialista ambiental"}]}
    rosa = member(60, profession="Ingeniera ambiental", id_=9) | {"name": "Rosa Quispe"}
    luis = member(18, profession="Ingeniero ambiental", id_=1) | {"name": "Luis"}
    result = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[luis],
                                  consortia=[group(partner_staff=[rosa])])
    status, reason = label_status(result, "Especialista ambiental")
    assert status == "consortium" and "Rosa Quispe (Petro Sur SAC)" in reason


def test_una_vez_el_valor_estimado():
    raw = {"valor_estimado": {"monto": 900000, "moneda": "PEN"}, "requisitos": [
        {"tipo": "experiencia", "descripcion": "Facturación", "veces_valor_estimado": 1, "cita": "una (1) vez"}]}
    facts = bases_facts.clean(raw, "una (1) vez el valor referencial S/ 900,000.00")
    result = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[])
    assert "(1 vez el valor estimado)" in label_status(result, "Facturación")[1]


def test_el_consorcio_carga_lo_que_piden_las_bases_analizadas():
    facts = {"requisitos": [
        {"tipo": "experiencia", "monto_minimo": 500000.0, "moneda": "PEN", "pagina": 30, "cita": "S/ 500,000.00"},
        {"tipo": "experiencia_anios", "anios": 5},
        {"tipo": "personal", "cantidad": 2}, {"tipo": "personal"}]}
    found = consortia.needs_from_facts(facts)
    assert found == {"amount": Decimal("500000.0"), "page": 30, "excerpt": "S/ 500,000.00", "years": 5, "staff": 3}
    assert consortia.needs_from_facts({"requisitos": [{"tipo": "experiencia", "monto_minimo": 1, "moneda": "USD"}]}) is None


def test_consorcios_de_la_empresa_con_el_personal_de_las_socias(account_factory):
    from services import accounts, staff
    lead = accounts.create_user(account_factory, "a@example.test", "Clave de prueba larga 2026", "Constructora")
    other = accounts.create_user(account_factory, "b@example.test", "Clave de prueba larga 2026", "Petro Sur SAC")
    for uid in (lead, other):
        accounts.save_profile(account_factory, uid, {"company_name": "X", "business_activity": "Obras", "rubros": "7214",
                                                     "monthly_sales": Decimal("1"), "experience_years": 3,
                                                     "similar_contracts_amount": Decimal("100"), "specialized_staff": ""})
    staff.add_member(account_factory, other, {"name": "Rosa Quispe", "profession": "Ingeniera ambiental", "specialty": "",
                                              "licensed": True, "experience_months": 60, "certifications": ""})
    cid = consortia.create(account_factory, lead, "Consorcio Norte Sur")
    assert eligibility.company_consortia(account_factory, lead) == []          # sin socias que aceptaron, no cuenta
    consortia.invite(account_factory, lead, cid, "b@example.test")
    invitation = consortia.detail(account_factory, lead, cid)["invitations"][0]["id"]
    consortia.respond(account_factory, other, "b@example.test", invitation, True)
    for uid in (lead, other):                                                  # la líder y la socia lo ven
        groups = eligibility.company_consortia(account_factory, uid)
        assert [g["name"] for g in groups] == ["Consorcio Norte Sur"] and len(groups[0]["members"]) == 2
    assert [p["name"] for p in eligibility.company_consortia(account_factory, lead)[0]["staff"]] == ["Rosa Quispe"]


# --------------------------------------------------------------------------- experiencia por objeto y rubro
WORKS = {"requisitos": [{"tipo": "experiencia", "descripcion": "Facturación en obras", "monto_minimo": 900000.0,
                         "moneda": "PEN", "cita": "S/ 900,000.00"}]}
SEVERAL = dict(PROFILE, categories=["bienes", "obras"], similar_contracts_amount=Decimal("1000000"))


def test_con_varios_objetos_el_total_no_basta_para_dar_verde():
    result = eligibility.evaluate(with_facts(WORKS), SEVERAL, [], {}, TODAY, members=[])
    status, reason = label_status(result, "Facturación en obras")
    assert status == "pending" and "suma todos sus objetos" in reason and "cuánto es de obras" in reason


def test_el_monto_de_cada_objeto_decide():
    ok = eligibility.evaluate(with_facts(WORKS), dict(SEVERAL, similar_amount_obras=Decimal("950000")), [], {}, TODAY,
                              members=[])
    status, reason = label_status(ok, "Facturación en obras")
    assert status == "pass" and "similares en obras (S/ 950,000.00)" in reason
    low = eligibility.evaluate(with_facts(WORKS), dict(SEVERAL, similar_amount_obras=Decimal("100000")), [], {}, TODAY,
                               members=[])
    assert label_status(low, "Facturación en obras")[0] == "short"


def test_si_declaro_los_otros_objetos_este_es_lo_que_queda_del_total():
    """Regresión (E2E 23/11/2026): bienes + obras declarados = total y servicios en blanco decía «indique cuánto
    es de servicios»; servicios es lo que queda (aquí S/ 0)."""
    services = {"requisitos": [dict(WORKS["requisitos"][0], descripcion="Facturación en servicios", monto_minimo=200000.0)]}
    three = dict(PROFILE, categories=["bienes", "servicios", "obras"], similar_contracts_amount=Decimal("1300000"),
                 similar_amount_bienes=Decimal("350000"), similar_amount_obras=Decimal("950000"))
    zero = eligibility.evaluate(with_facts(services, category="servicios"), three, [], {}, TODAY, members=[])
    status, reason = label_status(zero, "Facturación en servicios")
    assert status == "short" and "en servicios (S/ 0.00)" in reason and "lo que queda de su total" in reason
    rest = dict(three, similar_amount_obras=Decimal("700000"))                 # quedan S/ 250,000 para servicios
    status, reason = label_status(eligibility.evaluate(with_facts(services, category="servicios"), rest, [], {}, TODAY,
                                                       members=[]), "Facturación en servicios")
    assert status == "pass" and "S/ 250,000.00" in reason
    assert accounts_split(Decimal("100"), {"obras": Decimal("40")}, ["obras", "bienes"], "bienes") == (Decimal("60"), "resto")
    assert accounts_split(Decimal("100"), {}, ["obras", "bienes"], "bienes") == (Decimal("100"), "sin separar")


def accounts_split(*args):
    from services import accounts
    return accounts.similar_for_category(*args)


def test_con_un_solo_objeto_vale_el_total():
    result = eligibility.evaluate(with_facts(WORKS), PROFILE, [], {}, TODAY, members=[])     # PROFILE: solo obras
    assert label_status(result, "Facturación en obras")[0] == "pass"


def scope(codes=("1510",), partners=None):
    return {"codes": set(codes), "names": {"1510": "Combustibles", "7214": "Construcción de obras civiles"},
            "partners": partners or {}}


def test_fuera_de_sus_rubros_es_amarillo_y_la_experiencia_no_sale_verde_sola():
    tender = dict(with_facts(WORKS), rubros=["7214"])
    result = eligibility.evaluate(tender, PROFILE, [], {}, TODAY, members=[], scope=scope())
    rubro = next(c for c in result["checks"] if c["key"] == "rubro")
    assert rubro["status"] == "pending" and rubro["reason"].startswith("Es del rubro 7214 y en Mi empresa declara 1510.")
    assert "consorcio" in rubro["reason"] and "Ej.:" not in rubro["reason"]
    status, reason = label_status(result, "Facturación en obras")
    assert status == "pending" and "no es de sus rubros" in reason
    assert result["color"] == "amarillo"                                   # nunca rojo solo por el rubro


def test_de_su_rubro_no_hay_aviso():
    tender = dict(with_facts(WORKS), rubros=["7214"])
    result = eligibility.evaluate(tender, PROFILE, [], {}, TODAY, members=[], scope=scope(("7214",)))
    assert not any(c["key"] == "rubro" for c in result["checks"])
    assert label_status(result, "Facturación en obras")[0] == "pass"
    unknown = eligibility.evaluate(tender, PROFILE, [], {}, TODAY, members=[], scope=scope(()))   # sin rubros reconocidos
    assert not any(c["key"] == "rubro" for c in unknown["checks"])


def test_una_socia_del_rubro_lo_resuelve_en_consorcio():
    tender = dict(with_facts(WORKS), rubros=["7214"])
    partners = {1: {2: {"7214"}}}
    result = eligibility.evaluate(tender, PROFILE, [], {}, TODAY, members=[], consortia=[group()],
                                  scope=scope(partners=partners))
    rubro = next(c for c in result["checks"] if c["key"] == "rubro")
    assert rubro["status"] == "consortium" and "Petro Sur SAC" in rubro["reason"]


def test_rubros_de_mi_empresa_y_de_las_socias():
    found = eligibility.rubro_scope({"rubros": "1510, 7214", "business_activity": ""},
                                    [{"id": 5, "rubros": {2: {"rubros": "8110", "business_activity": ""}}}])
    assert found["codes"] == {"1510", "7214"} and found["partners"] == {5: {2: {"8110"}}}


def test_mi_empresa_guarda_el_monto_de_cada_objeto(account_client, account_factory):
    from services import accounts
    from test_categorias_servicios_obras import signed_in
    uid = signed_in(account_client, account_factory)
    form = {"company_name": "Constructora de prueba", "business_activity": "Obras", "rubros": "7214",
            "monthly_sales": "1000", "experience_years": "4", "similar_contracts_amount": "1000000",
            "specialized_staff": "Residente", "similar_amount_obras": "950000", "similar_amount_bienes": ""}
    account_client.post("/perfil", data=form)
    saved = accounts.get_profile(account_factory, uid)
    assert saved["similar_amount_obras"] == Decimal("950000") and saved["similar_amount_bienes"] is None
    response = account_client.post("/perfil", data=dict(form, similar_amount_obras="2000000"))
    assert "El total no puede ser menor" in response.get_data(as_text=True)
    assert "¿cuánto es de cada objeto?" in account_client.get("/perfil").get_data(as_text=True)


def test_en_consorcio_solo_suma_lo_facturado_en_ese_objeto():
    own = consortia.Member(company_id=1, name="Constructora", role="lider", similar_contracts_amount=Decimal("1000000"),
                           categories=["bienes", "obras"], similar_by_category={"obras": Decimal("300000")})
    fuel = consortia.Member(company_id=2, name="Petro Sur SAC", similar_contracts_amount=Decimal("600000"),
                            categories=["bienes"])
    builder = consortia.Member(company_id=3, name="Obras Andinas", similar_contracts_amount=Decimal("700000"),
                               categories=["obras", "servicios"])
    profile = dict(SEVERAL, similar_amount_obras=Decimal("300000"))
    only_fuel = {"id": 1, "name": "Con Petro Sur", "company_id": 1, "members": [own, fuel], "staff": []}
    result = eligibility.evaluate(with_facts(WORKS), profile, [], {}, TODAY, members=[], consortia=[only_fuel])
    assert label_status(result, "Facturación en obras")[0] == "short"       # el combustible no cuenta para obras
    with_builder = {"id": 2, "name": "Con Obras Andinas", "company_id": 1, "members": [own, builder], "staff": []}
    result = eligibility.evaluate(with_facts(WORKS), profile, [], {}, TODAY, members=[], consortia=[with_builder])
    status, reason = label_status(result, "Facturación en obras")
    assert status == "consortium" and "suman S/ 1,000,000.00" in reason and "Confirme cuánto de eso es de obras" in reason