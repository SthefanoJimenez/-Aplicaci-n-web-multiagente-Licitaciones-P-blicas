"""Criterios posteriores a la entrevista: identidad persistida, migración y fuentes de experiencia."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from services import accounts, assistant, db, eligibility, eligibility_chat, eligibility_store
from test_hu008 import PROFILE, register
from test_hu010 import business, public_db, prepared


@pytest.mark.parametrize('ruc', ['', '1234567890', '123456789012', '20-123456789', '20123 456789',
                                  'abcdefghijk', '２０１２３４５６７８９', '٢٠١٢٣٤٥٦٧٨٩'])
def test_profile_rejects_invalid_ruc_on_server(account_client, ruc):
    register(account_client)
    response = account_client.post('/perfil', data=PROFILE | {'ruc': ruc})
    assert response.status_code == 400
    assert 'data-error-field="ruc"' in response.text


def test_company_ruc_and_legal_name_persist_after_login(account_client, account_factory):
    register(account_client)
    assert account_client.post('/perfil', data=PROFILE).status_code == 302
    account_client.post('/salir')
    account_client.post('/ingresar', data={'email':'arturo@example.test','password':'Una frase de prueba 2026!'})
    response = account_client.get('/perfil')
    assert PROFILE['ruc'] in response.text and PROFILE['company_name'] in response.text
    with account_client.session_transaction() as session:
        assert PROFILE['ruc'] not in str(dict(session))
    saved = accounts.get_profile(account_factory,1)
    assert saved['ruc'] == PROFILE['ruc']


def test_duplicate_ruc_rolls_back_every_field_and_does_not_identify_other_account(account_client, account_factory):
    register(account_client)
    account_client.post('/perfil', data=PROFILE)
    account_client.post('/salir')
    register(account_client, email='segunda@example.test')
    own = PROFILE | {'ruc':'20987654321','company_name':'Segunda empresa'}
    assert account_client.post('/perfil', data=own).status_code == 302
    response = account_client.post('/perfil', data=PROFILE | {'company_name':'No guardar','monthly_sales':'1'})
    assert response.status_code == 400 and 'ya está asociado' in response.text
    assert 'arturo@example.test' not in response.text
    assert accounts.get_profile(account_factory,2)['company_name'] == own['company_name']
    assert accounts.get_profile(account_factory,2)['monthly_sales'] == 15000.50
    assert accounts.get_profile(account_factory,2)['ruc'] == own['ruc']
    assert accounts.get_profile(account_factory,1)['ruc'] == PROFILE['ruc']


def test_database_also_rejects_duplicate_ruc(account_factory):
    one = accounts.create_user(account_factory,'una@example.test','Solo prueba 1!','Una')
    two = accounts.create_user(account_factory,'dos@example.test','Solo prueba 2!','Dos')
    accounts.save_profile(account_factory,one,PROFILE)
    with pytest.raises(IntegrityError):
        with account_factory.begin() as session:
            session.scalar(select(accounts.Company).where(accounts.Company.user_id == two)).ruc = PROFILE['ruc']
    assert accounts.get_profile(account_factory,two)['ruc'] == ''


def test_additive_migration_preserves_legacy_login_and_private_bytes(tmp_path):
    engine = db.make_engine('sqlite:///' + str(tmp_path/'anterior.sqlite3'))
    hashed = generate_password_hash('Clave anterior 7!')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE usuarios (id INTEGER PRIMARY KEY, email VARCHAR(254) NOT NULL UNIQUE, password_hash VARCHAR(256) NOT NULL, created_at DATETIME NOT NULL)')
        connection.exec_driver_sql('CREATE TABLE empresas (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, name VARCHAR(200) NOT NULL)')
        connection.execute(text('INSERT INTO usuarios VALUES (1,:email,:hashed,:created)'),{'email':'anterior@example.test','hashed':hashed,'created':datetime.now(timezone.utc).isoformat()})
        connection.exec_driver_sql("INSERT INTO empresas VALUES (1,1,'Empresa anterior')")
        connection.exec_driver_sql('CREATE TABLE privado_preservar (content BLOB)')
        connection.execute(text('INSERT INTO privado_preservar VALUES (:content)'),{'content':b'PDF privado de prueba'})
    try:
        accounts.initialize_schema(engine)
        accounts.initialize_schema(engine)
        factory = db.make_session_factory(engine)
        assert accounts.authenticate(factory,'anterior@example.test','Clave anterior 7!') == 1
        assert accounts.get_profile(factory,1) == {'company_name':'Empresa anterior','ruc':''}
        accounts.save_profile(factory,1,PROFILE)
        assert accounts.get_profile(factory,1)['ruc'] == PROFILE['ruc']
        with engine.connect() as connection:
            assert connection.execute(text('SELECT content FROM privado_preservar')).scalar() == b'PDF privado de prueba'
    finally:
        engine.dispose()


def test_assistant_uses_identity_from_account_instead_of_chat(account_factory,business,public_db):
    values = accounts.get_profile(account_factory,business) | {'ruc':PROFILE['ruc'],'company_name':'Razón social guardada'}
    accounts.save_profile(account_factory,business,values)
    result = eligibility_chat.answer('Revisar convocatoria 9001',account_factory,business,
                                    profile={'ruc':'20987654321','company_name':'Empresa ajena'})
    assert result['profile']['ruc'] == values['ruc']
    assert result['profile']['company_name'] == values['company_name']
    assert result['evaluations'][0]['company'] == {'ruc':values['ruc'],'name':values['company_name']}
    assert values['ruc'] in result['response'] and values['company_name'] in result['response']
    assert '20987654321' not in result['response']


@pytest.mark.parametrize('amount', [1000, 300000, 9999999])
@pytest.mark.parametrize('has_rnp', [True,False])
def test_legacy_graph_does_not_invent_experience_or_pages_when_bases_are_missing(amount,has_rnp):
    result = assistant.evaluate_tenders_node({'profile':{'experience_amount':0,'has_rnp':has_rnp},
              'tenders_found':[{'code':'1','title':'Compra de combustible','rubros':'1510','estimated_value':amount,'chunks':[]}]})['evaluations'][0]
    assert result['semaphore'] == 'amarillo'
    assert result['page_ref'] is None
    assert 'umbral fijo' in ' '.join(result['reasons'])


@pytest.mark.parametrize('published', [1000, 300000, 9999999])
def test_experience_minimum_comes_from_bases_instead_of_tender_value(account_factory,business,public_db,published):
    with public_db.begin() as session:
        session.scalar(select(db.Tender).where(db.Tender.code=='9001')).estimated_value = published
    context = eligibility.load_tenders(['9001'])['9001']
    result = eligibility.evaluate(context,accounts.get_profile(account_factory,business),[],prepared(context),today=datetime(2026,11,11).date())
    source = next(check for check in result['checks'] if 'Experiencia mínima' in check.get('text',''))
    assert 'menor al mínimo' not in source['reason']  # 250000 supera los 200000 explícitos.
    with public_db.begin() as session:
        chunk = session.scalar(select(db.RequirementChunk).where(db.RequirementChunk.text.like('Experiencia mínima%')))
        chunk.text = 'Experiencia mínima: facturación acumulada de S/ 400,000.00 en bienes similares. No se permite consorcio.'
    changed = eligibility.load_tenders(['9001'])['9001']
    result = eligibility.evaluate(changed,accounts.get_profile(account_factory,business),[],prepared(changed),today=datetime(2026,11,11).date())
    assert result['color'] == 'rojo'
    assert any('mínimo de las bases (400000' in check['reason'] for check in result['checks'])
