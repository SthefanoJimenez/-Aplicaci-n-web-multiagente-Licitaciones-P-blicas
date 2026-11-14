"""HU-008: el acceso a evaluación reutiliza el perfil y recuerda el bien por empresa."""
import pytest
from sqlalchemy.exc import SQLAlchemyError

from agents import classifier
from services import accounts, data, llm
from test_hu008 import PROFILE, PASSWORD, register

CEMENT_I, CEMENT_IP = 12771, 12953


@pytest.fixture(autouse=True)
def no_external_model(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('La entrada desde el perfil no debe invocar un modelo externo.')
    monkeypatch.setattr(llm, 'generate_json', forbidden)
    monkeypatch.setattr(classifier, 'classify', forbidden)


def prepare(client):
    register(client)
    assert client.post('/perfil', data=PROFILE).status_code == 302


def offer(client, monkeypatch, codes=(CEMENT_I, CEMENT_IP)):
    monkeypatch.setattr(classifier, 'profile_candidates', lambda *args: [data.GOODS[c] for c in codes])
    return client.get('/evaluar/mi-producto')


def test_entry_requires_login(account_client):
    response = account_client.get('/evaluar/mi-producto')
    assert response.status_code == 302 and '/ingresar?' in response.location
    assert 'evaluar/mi-producto' in response.location


def test_entry_searches_saved_activity_and_rubros_without_copied_input(account_client, account_factory, monkeypatch):
    prepare(account_client)
    received = []
    def search(activity, rubros):
        received.append((activity, rubros))
        return [data.GOODS[CEMENT_I], data.GOODS[CEMENT_IP]]
    monkeypatch.setattr(classifier, 'profile_candidates', search)
    response = account_client.get('/evaluar/mi-producto?company_id=987&description=OTRA_EMPRESA')
    assert response.status_code == 200
    assert received == [(PROFILE['business_activity'], PROFILE['rubros'])]
    assert PROFILE['ruc'] in response.text and PROFILE['company_name'] in response.text
    assert 'OTRA_EMPRESA' not in response.text
    assert response.text.count('type="radio"') == 2
    assert 'checked' not in response.text.split('<fieldset class="candidates">')[1].split('</fieldset>')[0]
    assert accounts.get_company_product(account_factory, 1) is None
    with account_client.session_transaction() as session:
        content = str(dict(session))
        for field in ('ruc', 'company_name', 'business_activity', 'rubros', 'monthly_sales', 'specialized_staff'):
            assert str(PROFILE[field]) not in content
        assert 'code' not in session


def test_confirmed_product_survives_logout_and_goes_directly_to_market(account_client, account_factory, monkeypatch):
    prepare(account_client)
    offer(account_client, monkeypatch)
    response = account_client.post('/confirmar', data={'code':CEMENT_IP, 'user_id':'999', 'company_id':'999'})
    assert response.status_code == 302 and response.location.endswith('/mercado')
    assert accounts.get_company_product(account_factory, 1) == CEMENT_IP
    account_client.post('/salir')
    account_client.post('/ingresar', data={'email':'arturo@example.test','password':PASSWORD})
    def no_search(*args):
        pytest.fail('Un bien ya confirmado no debe volver a buscarse.')
    monkeypatch.setattr(classifier, 'profile_candidates', no_search)
    response = account_client.get('/evaluar/mi-producto')
    assert response.status_code == 302 and response.location.endswith('/mercado')
    market = account_client.get('/mercado')
    assert data.GOODS[CEMENT_IP]['name'] in market.text
    assert PROFILE['company_name'] in market.text
    assert 'Elegir otro producto de mi empresa' in market.text
    with account_client.session_transaction() as session:
        assert session['code'] == CEMENT_IP
        assert not any('cost' in key for key in session)


def test_cannot_remember_a_product_that_was_not_offered(account_client, account_factory, monkeypatch):
    prepare(account_client)
    offer(account_client, monkeypatch, [CEMENT_I])
    response = account_client.post('/confirmar', data={'code':CEMENT_IP})
    assert response.status_code == 302 and response.location.endswith('/evaluar')
    assert accounts.get_company_product(account_factory, 1) is None


def test_confirmation_still_requires_csrf(account_client, account_factory, monkeypatch):
    prepare(account_client)
    offer(account_client, monkeypatch)
    response = account_client.open('/confirmar', method='POST', data={'code':CEMENT_I})
    assert response.status_code == 400
    assert accounts.get_company_product(account_factory, 1) is None


def test_product_choice_is_separate_for_each_company(account_client, account_factory, monkeypatch):
    prepare(account_client)
    offer(account_client, monkeypatch)
    account_client.post('/confirmar', data={'code':CEMENT_I})
    account_client.post('/salir')
    register(account_client, email='otra@example.test')
    response = offer(account_client, monkeypatch, [CEMENT_IP])
    assert response.status_code == 200
    assert accounts.get_company_product(account_factory, 2) is None
    account_client.post('/confirmar', data={'code':CEMENT_IP, 'user_id':'1', 'company_id':'1'})
    assert accounts.get_company_product(account_factory, 1) == CEMENT_I
    assert accounts.get_company_product(account_factory, 2) == CEMENT_IP


def test_changing_activity_invalidates_old_choice_but_not_other_profile_fields(account_client, account_factory, monkeypatch):
    prepare(account_client)
    offer(account_client, monkeypatch)
    account_client.post('/confirmar', data={'code':CEMENT_I})
    assert account_client.post('/perfil', data=PROFILE | {'monthly_sales':'19','company_name':'Nuevo nombre'}).status_code == 302
    assert accounts.get_company_product(account_factory, 1) == CEMENT_I
    assert account_client.post('/perfil', data=PROFILE | {'rubros':'Vehículos'}).status_code == 302
    assert accounts.get_company_product(account_factory, 1) is None
    assert offer(account_client, monkeypatch, [CEMENT_IP]).status_code == 200
    with account_client.session_transaction() as session:
        assert 'code' not in session


def test_change_product_does_not_erase_previous_choice_until_confirmation(account_client, account_factory, monkeypatch):
    prepare(account_client)
    offer(account_client, monkeypatch)
    account_client.post('/confirmar', data={'code':CEMENT_I})
    assert account_client.get('/evaluar/mi-producto?cambiar=1').status_code == 200
    assert accounts.get_company_product(account_factory, 1) == CEMENT_I
    account_client.post('/confirmar', data={'code':CEMENT_IP})
    assert accounts.get_company_product(account_factory, 1) == CEMENT_IP


def test_incomplete_profile_has_manual_fallback(account_client, account_factory):
    register(account_client)
    response = account_client.get('/evaluar/mi-producto')
    assert response.status_code == 200
    assert 'Indique el bien que desea evaluar' in response.text
    assert 'name="description"' in response.text and 'Actualizar mi perfil' in response.text
    assert accounts.get_company_product(account_factory, 1) is None


def test_removed_catalog_code_is_not_reused(account_client, account_factory, monkeypatch):
    prepare(account_client)
    accounts.remember_company_product(account_factory, 1, 999999999)
    assert offer(account_client, monkeypatch).status_code == 200
    with account_client.session_transaction() as session:
        assert 'code' not in session


def test_failure_to_save_does_not_advance_or_expose_database_error(account_client, account_factory, monkeypatch):
    prepare(account_client)
    offer(account_client, monkeypatch)
    def fail(*args):
        raise SQLAlchemyError('PRIVATE_DATABASE_PASSWORD')
    monkeypatch.setattr(accounts, 'remember_company_product', fail)
    response = account_client.post('/confirmar', data={'code':CEMENT_I})
    assert response.status_code == 503 and 'PRIVATE_DATABASE_PASSWORD' not in response.text
    assert accounts.get_company_product(account_factory, 1) is None
    with account_client.session_transaction() as session:
        assert 'code' not in session


def test_local_profile_search_rejects_empty_generic_and_no_overlap():
    for activity, rubros in [('', ''), ('Venta al por mayor', ''), ('zzzzzzzzzzzz', 'xxxxxxxxxxxx')]:
        assert classifier.profile_candidates(activity, rubros) == []
    found = classifier.profile_candidates('Venta de cemento', 'cemento portland en bolsa de 42.5 kg')
    assert found and len(found) <= 3
    assert all(item['code'] in data.VALID_CODES for item in found)
    assert any('CEMENTO' in item['name'].upper() for item in found)
