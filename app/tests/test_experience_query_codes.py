from decimal import Decimal
import pytest
from services.assistant import _query_experience


@pytest.mark.parametrize("query", [
    "¿Qué experiencia exige la convocatoria 9100103?",
    "Qué facturación exige el proceso 9100101",
    "Experiencia del código 9100102",
])
def test_process_reference_is_not_declared_experience(query):
    assert _query_experience(query) is None


def test_own_amount_is_preserved_alongside_process_reference():
    assert _query_experience("Tengo S/ 250000 de experiencia; evalúa la convocatoria 9100103") == Decimal('250000')
