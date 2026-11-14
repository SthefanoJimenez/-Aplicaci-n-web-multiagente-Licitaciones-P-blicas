"""Reglas de HU-010, datos reales del recorrido HTTP y regresiones de privacidad."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pandas as pd
import pytest
from sqlalchemy import select, text

from app import app
from services import accounts, company_documents, data, db, eligibility, eligibility_chat, eligibility_store, guarantees, ingest, ocds_source, tenders

TODAY = date(2026, 11, 11)
DEADLINE = datetime(2026, 11, 30, 20, tzinfo=timezone.utc)


@pytest.fixture
def business(account_factory, monkeypatch):
    monkeypatch.setattr(company_documents, "today", lambda: TODAY)
    user = accounts.create_user(account_factory, "elegible@example.test", "Solo prueba 7!", "Empresa de prueba")
    accounts.save_profile(account_factory, user, {
        "ruc": "20123456789", "company_name": "Empresa de prueba", "business_activity": "Venta de barras de acero y cemento",
        "rubros": "3011", "monthly_sales": Decimal("15000.50"), "experience_years": 5,
        "similar_contracts_amount": Decimal("250000"), "specialized_staff": "Dos técnicos especializados",
    })
    with account_factory.begin() as session:
        company = session.scalar(select(accounts.Company).where(accounts.Company.user_id == user))
        for kind in ("rnp", "experiencia", "personal", "otro"):
            session.add(company_documents.CompanyDocument(company_id=company.id, filename=kind+".pdf",
                document_type=kind, valid_from=date(2026,1,1), expires_on=date(2026,12,31),
                size_bytes=10, page_count=1, sha256=kind, content=b"PDF privado de prueba"))
    return user


@pytest.fixture
def public_db(monkeypatch):
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    factory = db.make_session_factory(engine)
    tenders.use_session_factory(factory)
    with factory.begin() as session:
        run = db.IngestionRun(started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc), status="ok")
        session.add(run)
        session.flush()
        tender = db.Tender(ocid="hu010-9001", code="9001", title="Compra de barras de acero", description="Materiales de construcción",
            entity="Entidad de prueba", rubros="3011", state="vigente", estimated_value=300000, currency="PEN",
            published_at=datetime(2026,11,1,tzinfo=timezone.utc), deadline_at=DEADLINE, requirements_status="extraido", first_run_id=run.id, last_run_id=run.id)
        tender.documents.append(db.TenderDocument(title="Bases de prueba", url="https://www.gob.pe/bases-prueba.pdf", is_bases=True))
        for i, content in enumerate([
            "Presentar constancia de inscripción vigente en el RNP.",
            "Experiencia mínima: facturación acumulada de S/ 200,000.00 en bienes similares.",
            "Personal: acreditar 2 técnicos especializados mediante sus constancias.",
        ]):
            tender.chunks.append(db.RequirementChunk(chunk_index=i, text=content, page=i+4, source="texto", file_name="bases_prueba.pdf", embedding=[.1]*768))
        session.add(tender)
    yield factory
    tenders.use_session_factory(None)
    engine.dispose()


def prepared(context):
    return {"regime":"32069", "whole_contract":True, "guarantee_method":"retention", "rnp":"yes",
            "staff_count":2, "reviews":{r["key"]:"own" for r in context["requirements"]}}


def evaluate_for(factory, user, values=None):
    if values is not None:
        eligibility_store.save_inputs(factory, user, "9001", values)
    return eligibility.evaluate_many(factory, user, ["9001"], TODAY)["9001"]


def test_green_uses_saved_profile_owned_documents_bases_and_guarantee(account_factory, business, public_db):
    context = eligibility.load_tenders(["9001"])["9001"]
    result = evaluate_for(account_factory, business, prepared(context))
    assert result["color"] == "verde"
    assert result["amount"] == Decimal("300000")
    assert result["guarantee"]["amount"] == Decimal("30000.00")
    assert result["guarantee"]["retention_allowed"] is True
    assert all(check["status"] == "pass" for check in result["checks"])
    assert {check["page"] for check in result["checks"] if check.get("text")} == {4,5,6}


@pytest.mark.parametrize("expires,starts,expected", [
    (date(2026,11,29),date(2026,1,1),"expired"),
    (date(2026,11,30),date(2026,1,1),"current"),
    (date(2026,12,31),date(2026,12,1),"future"),
    (date(2026,12,31),date(2026,11,30),"current"),
])
def test_document_validity_is_checked_at_tender_deadline(account_factory, business, public_db, expires, starts, expected):
    with account_factory.begin() as session:
        doc = session.scalar(select(company_documents.CompanyDocument).where(company_documents.CompanyDocument.document_type=="rnp"))
        doc.expires_on, doc.valid_from = expires, starts
    context = eligibility.load_tenders(["9001"])["9001"]
    result = evaluate_for(account_factory, business, prepared(context))
    assert len(result["inventory"]["rnp"][expected]) == 1
    assert result["on_date"] == date(2026,11,30)
    assert result["color"] == ("verde" if expected=="current" else "amarillo")


def test_missing_document_stays_yellow_and_cannot_be_overridden_by_review(account_factory, business, public_db):
    context = eligibility.load_tenders(["9001"])["9001"]
    with account_factory.begin() as session:
        session.delete(session.scalar(select(company_documents.CompanyDocument).where(company_documents.CompanyDocument.document_type=="rnp")))
    result = evaluate_for(account_factory, business, prepared(context))
    assert result["color"] == "amarillo"
    assert any("RNP ausentes" in check["reason"] for check in result["checks"])


def test_explicit_no_rnp_is_red_even_when_old_pdf_exists(account_factory, business, public_db):
    context = eligibility.load_tenders(["9001"])["9001"]
    result = evaluate_for(account_factory, business, prepared(context)|{"rnp":"no"})
    assert result["color"] == "rojo"
    assert "no contar con el RNP" in result["reason"]


def test_consortium_is_yellow_only_with_declared_admissible_contribution(account_factory, business, public_db):
    profile = accounts.get_profile(account_factory,business)
    accounts.save_profile(account_factory,business,profile|{"similar_contracts_amount":Decimal("100000")})
    context = eligibility.load_tenders(["9001"])["9001"]
    values = prepared(context)
    exp_key = next(req["key"] for req in context["requirements"] if "Experiencia" in req["text"])
    values["reviews"][exp_key] = "consortium"
    doc = next(d for d in company_documents.list_documents(account_factory,business) if d["document_type"]=="experiencia")
    values |= {"consortium_confirmed":True,"partner_experience":"150000","partner_document":doc["id"]}
    result = evaluate_for(account_factory,business,values)
    assert result["color"] == "amarillo"
    assert any(c["status"]=="consortium" for c in result["checks"])
    result = evaluate_for(account_factory,business,values|{"partner_experience":"1000"})
    assert result["color"] == "rojo"


def test_consortium_cannot_override_an_explicit_prohibition(account_factory,business,public_db):
    profile = accounts.get_profile(account_factory,business)
    accounts.save_profile(account_factory,business,profile|{"similar_contracts_amount":Decimal("1000")})
    with public_db.begin() as session:
        chunk = session.scalar(select(db.RequirementChunk).where(db.RequirementChunk.chunk_index==1))
        chunk.text += " No se permite consorcio."
    context = eligibility.load_tenders(["9001"])["9001"]
    result = evaluate_for(account_factory,business,prepared(context)|{"consortium_confirmed":True,"partner_experience":"999999"})
    assert result["color"] == "rojo"


def test_source_change_invalidates_old_confirmation(account_factory,business,public_db):
    context = eligibility.load_tenders(["9001"])["9001"]
    values = prepared(context)
    assert evaluate_for(account_factory,business,values)["color"]=="verde"
    with public_db.begin() as session:
        chunk = session.scalar(select(db.RequirementChunk).where(db.RequirementChunk.chunk_index==1))
        chunk.text += " Se debe adjuntar el contrato completo."
    assert evaluate_for(account_factory,business)["color"]=="amarillo"


def test_unread_bases_never_produce_green(account_factory,business,public_db):
    context = eligibility.load_tenders(["9001"])["9001"]
    with public_db.begin() as session:
        session.scalar(select(db.Tender)).requirements_status="pendiente"
    assert evaluate_for(account_factory,business,prepared(context))["color"]=="amarillo"


def test_closed_tender_is_red(account_factory,business,public_db):
    with public_db.begin() as session:
        session.scalar(select(db.Tender)).state="no_vigente"
    context = eligibility.load_tenders(["9001"])["9001"]
    result = evaluate_for(account_factory,business,prepared(context))
    assert result["color"]=="rojo" and "ya no acepta ofertas" in result["reason"]


@pytest.fixture
def price_sample(monkeypatch):
    monkeypatch.setattr(data,"AWARDS",pd.DataFrame({"codigoitem":[1399]*6,"unidad_medida":["Unidad"]*6,
        "precio_unitario":[1,3,5,7,9,11],"anio":[2023,2024,2025,2023,2024,2025]}))


def test_amount_estimate_uses_quantity_exact_code_unit_and_median(price_sample):
    amount,details,_ = eligibility.historical_estimate([{"quantity":"12.5","catalog_code":1399,"unit":"Unidad"}])
    assert amount==Decimal("75.00") and details[0]["median"]==Decimal("6")
    amount,_,_=eligibility.historical_estimate([{"quantity":10,"catalog_code":1399,"unit":"Unidad"},
                                              {"quantity":5,"catalog_code":1399,"unit":"Unidad"}])
    assert amount==Decimal("90.00")


@pytest.mark.parametrize("item",[
    {"quantity":5,"catalog_code":1399,"unit":"Litro"},
    {"quantity":5,"catalog_code":999999999,"unit":"Unidad"},
    {"quantity":0,"catalog_code":1399,"unit":"Unidad"},
    {"quantity":"NaN","catalog_code":1399,"unit":"Unidad"},
    {"quantity":5,"classifications":[{"scheme":"UNSPSC","id":"1399"}],"unit":"Unidad"},
])
def test_missing_or_incompatible_estimate_input_does_not_invent_amount(price_sample,item):
    assert eligibility.historical_estimate([item])[0] is None


def test_missing_amount_is_yellow_with_literal_reason(account_factory,business,public_db):
    with public_db.begin() as session:
        row=session.scalar(select(db.Tender));row.estimated_value=None;row.items=[]
    context=eligibility.load_tenders(["9001"])["9001"]
    result=evaluate_for(account_factory,business,prepared(context))
    assert result["color"]=="amarillo" and result["amount"] is None
    assert "Falta el monto" in result["reason"]


def test_ingested_items_allow_automatic_estimation(account_factory,business,public_db,price_sample):
    with public_db.begin() as session:
        row=session.scalar(select(db.Tender));row.estimated_value=None
        row.items=[{"quantity":12.5,"classifications":[{"scheme":"CUBSO","id":"1399"}],"unit":{"name":"Unidad"}}]
    context=eligibility.load_tenders(["9001"])["9001"]
    result=evaluate_for(account_factory,business,prepared(context))
    assert result["amount"]==Decimal("75.00") and "OCDS" in result["amount_source"]


def test_guarantees_exemption_boundary_and_mype_retention():
    base={"regime":"32069","whole_contract":True,"guarantee_method":"retention"}
    exempt=guarantees.calculate(Decimal("275000"),base,TODAY)
    assert exempt["exempt"] and exempt["amount"]==0
    normal=guarantees.calculate(Decimal("275000.01"),base,TODAY)
    assert not normal["exempt"] and normal["amount"]==Decimal("27500.00")
    high=guarantees.calculate(Decimal("500000"),base,TODAY)
    assert high["status"]=="pending" and not high["retention_allowed"]
    mype=guarantees.calculate(Decimal("500000"),base|{"mype":True},TODAY)
    assert mype["status"]=="pass" and mype["amount"]==Decimal("50000.00")
    assert "no elimina ni reduce" in mype["reason"]


def test_accessory_and_advance_guarantees_are_separate_and_configurable():
    values={"regime":"32069","whole_contract":True,"guarantee_method":"coverage", "guarantee_capacity":"100000",
            "accessory_amount":"10000","advance_amount":"20000"}
    result=guarantees.calculate(Decimal("500000"),values,TODAY)
    assert result["amount"]==50000 and result["accessory"]==1000 and result["advance"]==20000
    custom=guarantees.calculate(Decimal("500000"),values,TODAY,guarantees.Policy(rate=Decimal("0.08")))
    assert custom["amount"]==40000
    retention=guarantees.calculate(Decimal("500000"),values|{"mype":True,"guarantee_method":"retention","guarantee_capacity":"0"},TODAY)
    assert retention["status"]=="pending"  # El adelanto sigue necesitando cobertura.


@pytest.mark.parametrize("changes",[{"regime":"30225"},{"regime":""},{"whole_contract":False},{"guarantee_method":""}])
def test_guarantees_wait_for_regime_and_aggregate_scope(changes):
    values={"regime":"32069","whole_contract":True}|changes
    assert guarantees.calculate(Decimal("500000"),values,TODAY)["status"]=="pending"
    assert guarantees.calculate(Decimal("500000"),values,date(2027,1,1))["status"]=="pending"


def test_new_columns_are_added_without_losing_existing_records(tmp_path):
    engine=db.make_engine("sqlite:///"+str(tmp_path/"old.sqlite3"));db.create_tables(engine)
    with engine.begin() as connection:
        # La integración debe migrar tanto los ítems de HU-010 como el cronograma de main.
        for name in ("items", "stage", "schedule", "enquiry_end"):
            connection.execute(text(f"ALTER TABLE convocatorias DROP COLUMN {name}"))
        connection.execute(text("INSERT INTO convocatorias (ocid,rubros,state,first_seen_at,requirements_status) VALUES ('conservada','','vigente','2026-11-11','pendiente')"))
    db.create_tables(engine)
    db.create_tables(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT ocid,items,stage,schedule,enquiry_end FROM convocatorias")).one()==("conservada",None,None,None,None)
    engine.dispose()


def login(client,factory,user):
    with client.session_transaction() as session:
        session["auth_token"]=accounts.begin_session(factory,user)


def form_values(context):
    return {"regime":"32069","whole_contract":"1","guarantee_method":"retention","rnp":"yes","staff_count":"2"} | {
        "review_"+r["key"]:"own" for r in context["requirements"]}


def test_real_forms_show_same_color_in_sheet_list_and_assistant(account_client,account_factory,business,public_db):
    login(account_client,account_factory,business)
    context=eligibility.load_tenders(["9001"])["9001"]
    response=account_client.get("/convocatorias/9001")
    assert response.status_code==200 and 'data-eligibility-color="amarillo"' in response.text
    response=account_client.post("/elegibilidad/9001",data=form_values(context),follow_redirects=True)
    assert response.status_code==200 and 'data-eligibility-color="verde"' in response.text
    listing=account_client.get("/convocatorias").text
    assert "Verde · Cumple por sí sola" in listing
    account_client.get("/asistente?convocatoria=9001")
    response=account_client.post("/asistente",data={"query":"Evalúa esta convocatoria"},follow_redirects=True)
    assert response.status_code==200 and "Verde · Convocatoria 9001" in response.text
    assert "página 5" in response.text and "200,000.00" in response.text
    assert "300,000.00" in response.text and "30,000.00" in response.text
    with account_client.session_transaction() as session:
        assert "assistant_history" not in session and "supplier_profile" not in session


def test_sheet_keeps_main_schedule_and_consortium_with_eligibility(account_client, account_factory, business, public_db):
    """El mismo recorrido conserva los módulos de main y la evaluación ya entregada."""
    with public_db.begin() as session:
        tender = session.scalar(select(db.Tender).where(db.Tender.code == "9001"))
        tender.stage = "integradas"
        tender.schedule_status = "leido"
        tender.schedule_read_at = datetime(2026, 11, 13, tzinfo=timezone.utc)
        tender.schedule_stage = "integradas"
        tender.schedule_due = False
        tender.schedule = [{"key": "ofertas", "name": "Presentación de ofertas", "start": "2026-11-21", "page": 2}]
    login(account_client, account_factory, business)
    context = eligibility.load_tenders(["9001"])["9001"]
    eligibility_store.save_inputs(account_factory, business, "9001", prepared(context))
    response = account_client.get("/convocatorias/9001")
    assert response.status_code == 200
    assert 'data-stage="integradas"' in response.text
    assert 'data-schedule-status="leido"' in response.text and "21 nov 2026" in response.text
    assert 'data-eligibility-color="verde"' in response.text
    assert "/consorcio?convocatoria=9001" in response.text
    # El enlace no debe quedar como adorno: el módulo de consorcios sigue registrado y operativo.
    response = account_client.post("/consorcio", data={"name": "Consorcio de integración"}, follow_redirects=True)
    assert response.status_code == 200 and "Consorcio de integración" in response.text
    assert "¿Juntas cubren lo que piden las bases?" in response.text


def test_account_isolation_of_declarations_documents_and_chat(account_client,account_factory,business,public_db):
    context=eligibility.load_tenders(["9001"])["9001"]
    evaluate_for(account_factory,business,prepared(context))
    eligibility_store.save_chat(account_factory,business,[{"role":"assistant","content":"Dato privado exclusivo"}],{"selected_tender_code":"9001"})
    other=accounts.create_user(account_factory,"otra@example.test","Otra clave 7!","Otra empresa")
    login(account_client,account_factory,other)
    assert "Dato privado exclusivo" not in account_client.get("/asistente").text
    response=account_client.get("/convocatorias/9001")
    assert 'data-eligibility-color="amarillo"' in response.text and "rnp.pdf" not in response.text
    doc_id=company_documents.list_documents(account_factory,business)[0]["id"]
    response=account_client.post("/elegibilidad/9001",data=form_values(context)|{"company_id":"1","partner_document":str(doc_id)},follow_redirects=True)
    assert "propia empresa" in response.text
    assert eligibility_store.load_inputs(account_factory,other,["9001"])=={}
    assert evaluate_for(account_factory,business)["color"]=="verde"


def test_reset_preserves_company_documents_and_saved_assessment(account_client,account_factory,business,public_db):
    login(account_client,account_factory,business)
    context=eligibility.load_tenders(["9001"])["9001"]
    evaluate_for(account_factory,business,prepared(context))
    account_client.post("/asistente",data={"query":"Convocatoria 9001"})
    assert eligibility_store.load_chat(account_factory,business)[0]
    assert account_client.post("/asistente/limpiar",follow_redirects=True).status_code==200
    assert eligibility_store.load_chat(account_factory,business)==([], {})
    assert len(company_documents.list_documents(account_factory,business))==4
    assert evaluate_for(account_factory,business)["color"]=="verde"
    assert accounts.get_profile(account_factory,business)["similar_contracts_amount"]==Decimal("250000")


def test_new_form_requires_login_and_csrf(account_factory,business,public_db):
    client=app.test_client()
    assert client.post("/elegibilidad/9001",data={}).status_code==302
    login(client,account_factory,business)
    assert client.post("/elegibilidad/9001",data={}).status_code==400


def test_chat_keeps_long_reply_out_of_cookie(account_client,account_factory,business,public_db,monkeypatch):
    login(account_client,account_factory,business)
    long="Respuesta completa de prueba "*350
    monkeypatch.setattr(eligibility_chat,"answer",lambda *a,**k:{"response":long,"profile":{}})
    response=account_client.post("/asistente",data={"query":"Consulta"},follow_redirects=True)
    assert response.status_code==200 and long[:500] in response.text
    assert eligibility_store.load_chat(account_factory,business)[0][-1]["content"]==long
    assert all(len(h)<4093 for h in response.headers.getlist("Set-Cookie"))


@pytest.mark.parametrize("value",["-1","NaN","Infinity","1e15","100,000","<script>"])
def test_invalid_numbers_cannot_be_saved(value):
    _,errors=eligibility.parse_form({"guarantee_capacity":value},[],[])
    assert errors


def test_ingestion_retains_quantities_units_and_codes(public_db):
    release={"ocid":"hu010-9002","date":"2026-11-01T00:00:00Z","buyer":{"name":"Entidad de prueba"},
        "tender":{"id":"9002","title":"Acero","mainProcurementCategory":"goods","status":"active",
            "items":[{"description":"Barra de acero","statusDetails":"CONVOCADO","quantity":12.5,"unit":{"name":"Unidad"},
                "classification":{"scheme":"CUBSO","id":"1399"},"additionalClassifications":[{"scheme":"UNSPSC","id":"3011"}]}]}}
    row=ocds_source.normalize_release(release)
    summary=ingest.run_ingestion(public_db, now=datetime(2026,11,11,tzinfo=timezone.utc),fetch=lambda *a:([row],None),rubros=["3011"])
    assert summary["status"]=="ok"
    stored=eligibility.load_tenders(["9002"])["9002"]["items"][0]
    assert stored["quantity"]==12.5 and stored["unit"]["name"]=="Unidad"
    assert any(c["scheme"]=="CUBSO" and c["id"]=="1399" for c in stored["classifications"])


def test_public_quantity_and_code_cannot_be_overridden_by_declarations(account_factory,business,public_db,price_sample):
    with public_db.begin() as session:
        row=session.scalar(select(db.Tender));row.estimated_value=None
        row.items=[{"quantity":12.5,"classifications":[{"scheme":"CUBSO","id":"1399"}],"unit":{"name":"Unidad"}}]
    context=eligibility.load_tenders(["9001"])["9001"]
    result=evaluate_for(account_factory,business,prepared(context)|{"quantity":"1","catalog_code":"1414"})
    assert result["amount"]==Decimal("75.00")


def test_multiple_items_cannot_be_replaced_with_partial_declaration(account_factory,business,public_db,price_sample):
    with public_db.begin() as session:
        row=session.scalar(select(db.Tender));row.estimated_value=None
        row.items=[{"quantity":q,"classifications":[{"scheme":"CUBSO","id":"1399"}],"unit":{"name":"Unidad"}} for q in (2,3)]
    context=eligibility.load_tenders(["9001"])["9001"]
    result=evaluate_for(account_factory,business,prepared(context)|{"quantity":"1","catalog_code":"1399"})
    assert result["amount"]==Decimal("30.00") and len(result["estimate"])==2


def test_changed_profile_and_removed_document_recalculate_existing_assessment(account_factory,business,public_db):
    context=eligibility.load_tenders(["9001"])["9001"]
    assert evaluate_for(account_factory,business,prepared(context))["color"]=="verde"
    profile=accounts.get_profile(account_factory,business)
    accounts.save_profile(account_factory,business,profile|{"similar_contracts_amount":Decimal("500")})
    assert evaluate_for(account_factory,business)["color"]=="amarillo"
    accounts.save_profile(account_factory,business,profile)
    doc=next(d for d in company_documents.list_documents(account_factory,business) if d["document_type"]=="personal")
    company_documents.delete_document(account_factory,business,doc["id"])
    assert evaluate_for(account_factory,business)["color"]=="amarillo"


def test_partner_rnp_is_not_experience_evidence(account_factory,business,public_db):
    context=eligibility.load_tenders(["9001"])["9001"]
    profile=accounts.get_profile(account_factory,business)
    accounts.save_profile(account_factory,business,profile|{"similar_contracts_amount":Decimal("100")})
    values=prepared(context)
    exp_key=next(req["key"] for req in context["requirements"] if "Experiencia" in req["text"])
    values["reviews"][exp_key]="consortium"
    doc=next(d for d in company_documents.list_documents(account_factory,business) if d["document_type"]=="rnp")
    result=evaluate_for(account_factory,business,values|{"partner_document":doc["id"],"partner_experience":"999999","consortium_confirmed":True})
    assert not any(c["status"]=="consortium" for c in result["checks"])


def test_new_evaluation_does_not_invoke_legacy_heuristic_or_external_model(account_factory,business,public_db,monkeypatch):
    from services import assistant, llm
    monkeypatch.setattr(assistant.ASSISTANT_GRAPH,"invoke",lambda *a,**k:pytest.fail("No usar heurísticas anteriores"))
    monkeypatch.setattr(llm,"get_client",lambda *a,**k:pytest.fail("No enviar datos financieros ni PDF a modelos"),raising=False)
    context=eligibility.load_tenders(["9001"])["9001"]
    evaluate_for(account_factory,business,prepared(context))
    answer=eligibility_chat.answer("¿Puedo postular a la 9001?",account_factory,business)
    assert answer["evaluations"][0]["color"]=="verde"


def test_conflicting_chat_amount_requires_profile_update(account_factory,business,public_db):
    result=eligibility_chat.answer("Tengo experiencia de S/ 1,000 y quiero revisar la convocatoria 9001",account_factory,business)
    assert result["evaluations"]==[] and "difiere de la guardada" in result["response"]
    assert accounts.get_profile(account_factory,business)["similar_contracts_amount"]==250000


def test_chat_rnp_declaration_changes_the_same_saved_tender_evaluation(account_factory,business,public_db):
    context=eligibility.load_tenders(["9001"])["9001"]
    evaluate_for(account_factory,business,prepared(context))
    answer=eligibility_chat.answer("No tengo RNP para la convocatoria 9001",account_factory,business)
    assert answer["evaluations"][0]["color"]=="rojo"
    assert evaluate_for(account_factory,business)["color"]=="rojo"


def test_unknown_guarantee_year_at_closing_stays_pending(account_factory,business,public_db):
    with public_db.begin() as session:
        session.scalar(select(db.Tender)).deadline_at=datetime(2027,1,2,20,tzinfo=timezone.utc)
    context=eligibility.load_tenders(["9001"])["9001"]
    result=evaluate_for(account_factory,business,prepared(context))
    assert result["guarantee"]["status"]=="pending" and result["guarantee"]["amount"] is None


@pytest.mark.parametrize("money",["20,000.00.","20.000,00.","20 000.00.","20  000.00. 2 anexos adicionales"])
def test_sales_requirement_uses_monthly_sales_from_saved_profile(account_factory,business,public_db,money):
    with public_db.begin() as session:
        row=session.scalar(select(db.Tender))
        row.chunks.append(db.RequirementChunk(chunk_index=3,text="Ventas mensuales mínimas de S/ "+money,page=7,source="texto",embedding=[.1]*768))
    context=eligibility.load_tenders(["9001"])["9001"]
    result=evaluate_for(account_factory,business,prepared(context))
    assert result["color"]=="amarillo" and any("ventas mensuales" in c["reason"] for c in result["checks"])


def test_staff_count_shortfall_cannot_be_overridden_by_confirmation(account_factory,business,public_db):
    context=eligibility.load_tenders(["9001"])["9001"]
    result=evaluate_for(account_factory,business,prepared(context)|{"staff_count":1})
    assert result["color"]=="amarillo" and any("cantidad de personal" in c["reason"] for c in result["checks"])


@pytest.mark.parametrize("phrase",["S/ 2 millones.","2 millones de soles.","S/ 2,000,000.00."])
def test_large_experience_amount_is_not_read_as_two_soles(account_factory,business,public_db,phrase):
    with public_db.begin() as session:
        chunk=session.scalar(select(db.RequirementChunk).where(db.RequirementChunk.chunk_index==1))
        chunk.text="Experiencia mínima: facturación acumulada de "+phrase
    context=eligibility.load_tenders(["9001"])["9001"]
    result=evaluate_for(account_factory,business,prepared(context))
    assert result["color"]=="amarillo"
