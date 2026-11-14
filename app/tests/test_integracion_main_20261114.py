"""Cruces entre HU-010 y el chat/consorcios y cuentas actuales de main."""
from datetime import date

import pytest
from sqlalchemy import select

from app import app
from services import accounts, assistant, company_documents, db, eligibility, eligibility_store
from test_hu010 import business, public_db, prepared, login


@pytest.mark.parametrize('start,end,color,reason', [
    (date(2026,1,1), date(2026,11,13), 'amarillo', 'vencidos'),
    (date(2027,1,1), date(2027,12,31), 'amarillo', 'aún no vigentes'),
    (date(2026,1,1), date(2027,12,31), 'verde', 'Cumple por sí sola'),
])
def test_personal_same_verdict_in_widget_sheet_and_list(account_client, account_factory, business, public_db, monkeypatch, start, end, color, reason):
    from services import llm
    monkeypatch.setattr(llm, 'generate_json', lambda _:None)
    context = eligibility.load_tenders(['9001'])['9001']
    eligibility_store.save_inputs(account_factory, business, '9001', prepared(context))
    with account_factory.begin() as session:
        doc = session.scalar(select(company_documents.CompanyDocument).where(company_documents.CompanyDocument.document_type == 'personal'))
        doc.valid_from, doc.expires_on = start, end
    login(account_client, account_factory, business)
    assessment = eligibility.evaluate_many(account_factory, business, ['9001'])['9001']
    assert assessment['color'] == color
    assert f'data-eligibility-color="{color}"' in account_client.get('/convocatorias/9001').text
    assert f'{color.capitalize()} · {assessment["label"]}' in account_client.get('/convocatorias').text
    # Una pregunta sobre documentos de un código concreto también usa la fecha de esa convocatoria.
    body = account_client.post('/asistente/mensaje', data={'query':'Evalúa los documentos de la convocatoria 9001'}).get_json()
    assert body['ok'] and reason in body['detail']
    assert assessment['label'] in body['detail'] and str(assessment['on_date']) in body['detail']
    assert eligibility_store.load_chat(account_factory, business)[0]
    with account_client.session_transaction() as session:
        assert 'assistant_history' not in session


def test_conflicting_amount_does_not_replace_profile_in_widget(account_factory, business):
    before = accounts.get_profile(account_factory, business)
    result = assistant.ask_assistant('Tengo experiencia de S/ 1000. Convocatoria 9001', business, account_factory, call_model=lambda _:pytest.fail('No redactar un veredicto'))
    assert result['status'] == 'profile_conflict' and not result['evaluations']
    assert accounts.get_profile(account_factory, business) == before


def test_ruc_from_chat_cannot_take_other_account_identity(account_factory):
    one = accounts.create_user(account_factory,'una-ruc@example.test','Una clave larga!','Una')
    two = accounts.create_user(account_factory,'dos-ruc@example.test','Otra clave larga!','Dos')
    assert accounts.save_ruc(account_factory, one, '20123456789')
    assert not accounts.save_ruc(account_factory, two, '20123456789')
    assert accounts.get_profile(account_factory, two)['ruc'] == ''
    assert not accounts.save_ruc(account_factory, one, '20987654321')
    assert accounts.get_profile(account_factory, one)['ruc'] == '20123456789'


def test_legacy_duplicate_ruc_migration_preserves_accounts(tmp_path, caplog):
    engine = db.make_engine('sqlite:///' + str(tmp_path/'legacy-main.sqlite3'))
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE empresas (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, name VARCHAR(200) NOT NULL, ruc VARCHAR(11))')
        connection.exec_driver_sql("INSERT INTO empresas VALUES (1,1,'Una','20123456789'), (2,2,'Dos','20123456789')")
    accounts.initialize_schema(engine)
    accounts.initialize_schema(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT id,name,ruc FROM empresas ORDER BY id').all() == [(1,'Una','20123456789'),(2,'Dos','20123456789')]
    assert 'duplicados anteriores' in caplog.text
    engine.dispose()


def test_offer_schedule_is_used_when_api_has_no_deadline(account_factory, business, public_db):
    with public_db.begin() as session:
        row = session.scalar(select(db.Tender).where(db.Tender.code == '9001'))
        row.deadline_at = None
        row.schedule = [{"key":"ofertas", "start":"2026-11-24", "end":None, "page":2}]
    context = eligibility.load_tenders(['9001'])['9001']
    result = eligibility.evaluate(context, accounts.get_profile(account_factory, business), company_documents.list_documents(account_factory, business), prepared(context), today=date(2026,11,14))
    assert result['on_date'] == date(2026,11,24)
    assert 'cronograma de las bases' in result['date_basis']
    expired = eligibility.evaluate(context, accounts.get_profile(account_factory, business), [], today=date(2026,11,25))
    assert expired['color'] == 'rojo'


@pytest.mark.parametrize('has_deadline', [True, False])
def test_awarded_tender_is_excluded_and_direct_sheet_never_green(account_client, account_factory, business, public_db, monkeypatch, has_deadline):
    from services import llm, schedule, tenders
    monkeypatch.setattr(llm, 'generate_json', lambda _:None)
    context = eligibility.load_tenders(['9001'])['9001']
    eligibility_store.save_inputs(account_factory, business, '9001', prepared(context))
    with public_db.begin() as session:
        row = session.scalar(select(db.Tender).where(db.Tender.code == '9001'))
        row.stage = schedule.BUENA_PRO
        if not has_deadline:
            row.deadline_at = None
    login(account_client, account_factory, business)
    assert not tenders.open_tenders()
    listing = account_client.get('/convocatorias').text
    assert 'Compra de barras de acero' not in listing
    result = eligibility.evaluate_many(account_factory, business, ['9001'])['9001']
    assert result['color'] == 'rojo'
    assert any(c['key'] == 'etapa' and c['status'] == 'fail' for c in result['checks'])
    page = account_client.get('/convocatorias/9001')
    assert page.status_code == 200 and 'data-eligibility-color="rojo"' in page.text
    assert 'buena pro otorgada' in page.text
    answer = assistant.ask_assistant('Convocatoria 9001', business, account_factory, call_model=lambda _:None)
    assert answer['status'] == 'no_tenders' and not answer['evaluations']


def test_schedule_and_stage_remain_available_before_items_migration(public_db):
    from services import schedule
    with public_db.begin() as session:
        row = session.scalar(select(db.Tender).where(db.Tender.code == '9001'))
        row.stage = schedule.BUENA_PRO
        row.deadline_at = None
        row.schedule = [{'key':'ofertas','start':'2026-11-24'}]
    with public_db.kw['bind'].begin() as connection:
        connection.exec_driver_sql('ALTER TABLE convocatorias DROP COLUMN items')
    context = eligibility.load_tenders(['9001'])['9001']
    assert context['stage'] == schedule.BUENA_PRO
    assert context['offer_day'] == date(2026,11,24)
    assert context['items'] == []
