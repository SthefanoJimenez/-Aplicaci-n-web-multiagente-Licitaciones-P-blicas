"""HU-012: declarar un consorcio con otras empresas registradas.

Suma de capacidades, evaluación para la empresa sola y para el consorcio, invitaciones con
aceptación y aislamiento entre empresas. Usa SQLite temporal y empresas sintéticas.
"""
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import app
from conftest import FormClient
from services import accounts, consortia
from services.requirements import Block, Requirements

PASSWORD = "Una frase de prueba 2026"


def member(cid, name, sales="0", amount="0", years=0, staff="", has_profile=True):
    return consortia.Member(company_id=cid, name=name, has_profile=has_profile, monthly_sales=Decimal(sales),
                            similar_contracts_amount=Decimal(amount), experience_years=years, staff=staff)


def company(factory, email, name, sales="0", amount="0", years=0, staff="Ingeniero químico colegiado", profile=True):
    """Crea la cuenta, guarda su perfil y devuelve un cliente con la sesión iniciada."""
    uid = accounts.create_user(factory, email, PASSWORD, name)
    if profile:
        accounts.save_profile(factory, uid, {
            "company_name": name, "ruc": "20123456789",
            "business_activity": "Venta de combustibles", "rubros": "Combustibles",
            "monthly_sales": Decimal(sales), "experience_years": years,
            "similar_contracts_amount": Decimal(amount), "specialized_staff": staff})
    client = FormClient(app, app.response_class)
    with client.session_transaction() as sess:
        sess["auth_token"] = accounts.begin_session(factory, uid)
    return client


def invitation_id(factory, email):
    with factory() as db:
        return db.scalar(select(consortia.ConsortiumMember.id).where(consortia.ConsortiumMember.invited_email == email))


def result_cell(html, which):
    """Texto del estado en la fila «Resultado» (sola o consorcio)."""
    start = html.index(f'data-result="{which}"')
    return html[start:html.index("</td>", start)]


# ---------------------------------------------------------------------------
# Suma de capacidades y evaluación (sin base de datos)
# ---------------------------------------------------------------------------

def test_caso_de_aceptacion_30_30_y_50_mil_cubren_una_base_de_100_mil():
    members = [member(1, "Empresa A", sales="30000"), member(2, "Empresa B", sales="30000"),
               member(3, "Empresa C", sales="50000")]
    result = consortia.evaluate(members, {"monthly_sales": Decimal("100000")}, own_company_id=1)
    row = result["rows"][0]
    assert row["total"] == Decimal("110000") and row["total_meets"]
    assert not any(c["meets"] for c in row["contributions"])       # ninguna la cubre por separado
    assert result["consortium_meets"] and not result["alone_meets"] and result["nobody_alone"]
    assert [round(c["share"], 1) for c in row["contributions"]] == [27.3, 27.3, 45.5]
    assert row["own_value"] == Decimal("30000") and not row["own_meets"]


def test_suma_ventas_facturado_y_personal_pero_toma_el_mayor_de_los_anios():
    members = [member(1, "A", sales="10000", amount="80000", years=3, staff="Ingeniero\nTécnico"),
               member(2, "B", sales="5000", amount="40000", years=7, staff="- Químico")]
    totals = consortia.combine(members)["totals"]
    assert totals == {"monthly_sales": Decimal("15000"), "similar_contracts_amount": Decimal("120000"),
                      "experience_years": 7, "staff_count": 3}


def test_una_integrante_sin_perfil_no_se_suma_y_se_avisa():
    members = [member(1, "A", sales="60000"), member(2, "Sin perfil", has_profile=False)]
    result = consortia.evaluate(members, {"monthly_sales": Decimal("50000")}, own_company_id=2)
    assert result["rows"][0]["total"] == Decimal("60000")
    assert result["missing_profiles"] == ["Sin perfil"] and not result["own_has_profile"]
    assert result["rows"][0]["own_value"] is None and not result["alone_meets"]


def test_sin_requisitos_no_hay_evaluacion_y_cuando_la_empresa_sola_cumple_se_indica():
    members = [member(1, "A", amount="200000", years=5), member(2, "B", amount="10000")]
    assert consortia.evaluate(members, {}, own_company_id=1) is None
    result = consortia.evaluate(members, {"similar_contracts_amount": Decimal("150000"), "experience_years": 2}, 1)
    assert result["alone_meets"] and result["consortium_meets"] and not result["nobody_alone"]


def test_personal_cuenta_lineas_y_no_cuenta_la_frase_sin_personal():
    assert consortia.staff_lines("No cuento con personal especializado") == []
    assert consortia.staff_lines("1. Ingeniero químico\n\n• Técnico en seguridad\n") == ["Ingeniero químico", "Técnico en seguridad"]


def test_lee_el_monto_facturado_de_los_requisitos_de_las_bases():
    text = ("B. EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD\nRequisito: El postor debe acreditar un monto facturado "
            "acumulado equivalente a S/ 150,000.00 por la venta\nde bienes iguales o similares.")
    reqs = Requirements(status="extraido", available=True, blocks=[Block(page=None, text="A. CAPACIDAD LEGAL"),
                                                                       Block(page=37, text=text)])
    found = consortia.amount_from_requirements(reqs)
    assert found["amount"] == Decimal("150000.00") and found["page"] == 37
    assert "monto facturado acumulado" in found["excerpt"]
    assert consortia.amount_from_requirements(Requirements(status="pendiente", available=False)) is None
    assert consortia.amount_from_requirements(Requirements(status="extraido", available=True,
                                                           blocks=[Block(page=1, text="Sin montos")])) is None


@pytest.mark.parametrize("raw, expected", [("150,000.00", "150000.00"), ("150 000,00", "150000.00"),
                                           ("150.000", "150000"), ("1,250,000", "1250000"), ("98500", "98500"),
                                           ("75,5", "75.5")])
def test_montos_de_las_bases_en_distintos_formatos(raw, expected):
    assert consortia.parse_pen(raw) == Decimal(expected)


def test_valida_los_requisitos_escritos_por_el_usuario():
    values, required, errors = consortia.parse_required({"ventas": "100000", "facturado": "1,5", "anios": "300",
                                                         "personal": ""})
    assert required == {"monthly_sales": Decimal("100000"), "similar_contracts_amount": Decimal("1.5")}
    assert set(errors) == {"anios"} and values["anios"] == "300"
    assert consortia.parse_required({"facturado": "100.000,00"})[2] == {
        "facturado": "Escriba un monto sin separadores de miles y con hasta dos decimales."}


# ---------------------------------------------------------------------------
# Pantallas: crear, invitar, aceptar y evaluar
# ---------------------------------------------------------------------------

def test_flujo_completo_tres_empresas_que_solo_juntas_cubren_la_base(account_factory):
    leader = company(account_factory, "a@example.test", "Combustibles Andinos SAC", sales="30000")
    partner_b = company(account_factory, "b@example.test", "Grifos del Norte SAC", sales="30000")
    partner_c = company(account_factory, "c@example.test", "Petro Sur EIRL", sales="50000")

    assert "Consorcios" in leader.get("/perfil").text
    created = leader.post("/consorcio", data={"name": "Consorcio Norte"})
    assert created.status_code == 302 and created.location == "/consorcio/1"
    for email in ("b@example.test", "C@Example.test "):
        assert leader.post("/consorcio/1/invitar", data={"email": email}).status_code == 302
    page = leader.get("/consorcio/1").text
    assert "b@example.test" in page and "c@example.test" in page and "Invitación pendiente" in page
    assert "Grifos del Norte SAC" not in page          # sin aceptar, la líder no ve sus datos

    inbox = partner_b.get("/consorcio").text
    assert "Consorcio Norte" in inbox and "Combustibles Andinos SAC" in inbox
    assert partner_b.post("/consorcio/invitaciones/1/aceptar").status_code == 302
    assert partner_c.post("/consorcio/invitaciones/2/aceptar").status_code == 302

    evaluated = leader.get("/consorcio/1?ventas=100000")
    assert evaluated.status_code == 200 and evaluated.headers["Cache-Control"] == "no-store"
    html = evaluated.text
    assert "Grifos del Norte SAC" in html and "Petro Sur EIRL" in html
    assert "S/ 110,000.00" in html
    assert "No cumple" in result_cell(html, "alone") and "Cumple" in result_cell(html, "consortium")
    assert "No cumple" not in result_cell(html, "consortium")
    assert "Ninguna integrante cubre estos requisitos por separado, pero el consorcio sí." in html
    assert "45.5%" in html and "27.3%" in html

    # Una integrante también ve el consorcio y su propia evaluación
    member_view = partner_c.get("/consorcio/1?ventas=100000").text
    assert "Su empresa es integrante" in member_view and "Petro Sur EIRL (su empresa)" in member_view
    assert "b@example.test" not in member_view          # los correos invitados solo los ve la líder
    assert "Consorcio Norte" in partner_c.get("/consorcio").text


def test_aislamiento_entre_empresas(account_factory):
    leader = company(account_factory, "a@example.test", "Líder SAC", sales="30000")
    invited = company(account_factory, "b@example.test", "Invitada SAC", sales="30000")
    outsider = company(account_factory, "x@example.test", "Ajena SAC", sales="90000")
    leader.post("/consorcio", data={"name": "Consorcio Privado"})
    leader.post("/consorcio/1/invitar", data={"email": "b@example.test"})

    assert outsider.get("/consorcio/1").status_code == 404
    assert invited.get("/consorcio/1").status_code == 404           # pendiente: aún no integra
    assert outsider.post("/consorcio/invitaciones/1/aceptar").status_code == 404   # no es su correo
    assert outsider.post("/consorcio/1/invitar", data={"email": "x@example.test"}).status_code == 404
    assert outsider.get("/consorcio/1/eliminar").status_code == 404
    assert outsider.post("/consorcio/1/integrantes/1/quitar").status_code == 404
    assert "Consorcio Privado" not in outsider.get("/consorcio").text

    invited.post("/consorcio/invitaciones/1/aceptar")
    assert invited.get("/consorcio/1").status_code == 200
    # Una integrante no puede invitar ni eliminar el consorcio
    assert invited.post("/consorcio/1/invitar", data={"email": "x@example.test"}).status_code == 404
    assert invited.get("/consorcio/1/eliminar").status_code == 404
    with account_factory() as db:
        assert db.scalar(select(func.count()).select_from(consortia.ConsortiumMember)) == 1


def test_validaciones_al_crear_e_invitar(account_factory):
    leader = company(account_factory, "a@example.test", "Líder SAC")
    assert leader.post("/consorcio", data={"name": "ab"}).status_code == 400
    leader.post("/consorcio", data={"name": "Consorcio Uno"})
    own = leader.post("/consorcio/1/invitar", data={"email": "A@example.test"})
    assert own.status_code == 400 and "su empresa ya es la líder" in own.text
    assert leader.post("/consorcio/1/invitar", data={"email": "no-es-correo"}).status_code == 400
    assert leader.post("/consorcio/1/invitar", data={"email": "b@example.test"}).status_code == 302
    repeated = leader.post("/consorcio/1/invitar", data={"email": "b@example.test"})
    assert repeated.status_code == 400 and "ya está invitado" in repeated.text
    bad = leader.get("/consorcio/1?anios=abc")
    assert bad.status_code == 400 and "Escriba un número entero de 0 a 200." in bad.text


def test_una_invitacion_a_un_correo_sin_cuenta_espera_hasta_el_registro(account_factory):
    leader = company(account_factory, "a@example.test", "Líder SAC", amount="60000")
    leader.post("/consorcio", data={"name": "Consorcio Futuro"})
    assert leader.post("/consorcio/1/invitar", data={"email": "nueva@example.test"}).status_code == 302
    newcomer = company(account_factory, "nueva@example.test", "Nueva SAC", amount="50000")
    assert "Consorcio Futuro" in newcomer.get("/consorcio").text
    newcomer.post("/consorcio/invitaciones/1/aceptar")
    html = leader.get("/consorcio/1?facturado=100000").text
    assert "S/ 110,000.00" in html and "Cumple" in result_cell(html, "consortium")


def test_rechazar_reinvitar_retirarse_quitar_y_eliminar(account_factory):
    leader = company(account_factory, "a@example.test", "Líder SAC")
    partner = company(account_factory, "b@example.test", "Socia SAC")
    leader.post("/consorcio", data={"name": "Consorcio Cambios"})
    leader.post("/consorcio/1/invitar", data={"email": "b@example.test"})

    assert partner.post("/consorcio/invitaciones/1/rechazar").status_code == 302
    assert "Rechazó la invitación" in leader.get("/consorcio/1").text
    assert partner.post("/consorcio/invitaciones/1/aceptar").status_code == 404     # ya respondió
    assert leader.post("/consorcio/1/invitar", data={"email": "b@example.test"}).status_code == 302
    partner.post("/consorcio/invitaciones/1/aceptar")
    assert "Socia SAC" in leader.get("/consorcio/1").text

    assert partner.post("/consorcio/1/integrantes/1/quitar").status_code == 302      # se retira
    assert partner.get("/consorcio/1").status_code == 404
    leader.post("/consorcio/1/invitar", data={"email": "b@example.test"})
    row = invitation_id(account_factory, "b@example.test")     # SQLite puede reutilizar el id borrado
    partner.post(f"/consorcio/invitaciones/{row}/aceptar")
    assert leader.post(f"/consorcio/1/integrantes/{row}/quitar").status_code == 302  # la líder la quita
    assert partner.get("/consorcio/1").status_code == 404

    confirmation = leader.get("/consorcio/1/eliminar")
    assert confirmation.status_code == 200 and "Conservar consorcio" in confirmation.text
    assert leader.post("/consorcio/1/eliminar").status_code == 302
    assert leader.get("/consorcio/1").status_code == 404
    with account_factory() as db:
        assert db.scalar(select(func.count()).select_from(consortia.Consortium)) == 0
        assert db.scalar(select(func.count()).select_from(consortia.ConsortiumMember)) == 0


def test_empresa_sin_perfil_completo_no_se_suma(account_factory):
    leader = company(account_factory, "a@example.test", "Líder SAC", sales="80000")
    partner = company(account_factory, "b@example.test", "Sin Perfil SAC", profile=False)
    leader.post("/consorcio", data={"name": "Consorcio Incompleto"})
    leader.post("/consorcio/1/invitar", data={"email": "b@example.test"})
    partner.post("/consorcio/invitaciones/1/aceptar")
    html = leader.get("/consorcio/1?ventas=100000").text
    assert "Perfil incompleto: no se suma" in html
    assert "No se sumó a Sin Perfil SAC porque su perfil está incompleto." in html


def test_consorcio_requiere_sesion_y_formulario_valido(account_factory, account_client):
    response = account_client.get("/consorcio")
    assert response.status_code == 302 and response.location.startswith("/ingresar")
    leader = company(account_factory, "a@example.test", "Líder SAC")
    no_token = leader.open("/consorcio", method="POST", data={"name": "Sin token"})
    assert no_token.status_code == 400
    with account_factory() as db:
        assert db.scalar(select(func.count()).select_from(consortia.Consortium)) == 0


def test_desde_una_convocatoria_carga_el_monto_facturado_de_sus_bases(account_factory, monkeypatch):
    from types import SimpleNamespace
    from services import tenders
    text = "Requisito: El postor debe acreditar un monto facturado acumulado equivalente a S/ 150,000.00 por la venta"
    reqs = Requirements(status="extraido", available=True, blocks=[Block(page=37, text=text)])
    tender = SimpleNamespace(code="1001", title="Adquisición de diésel B5", description=None)
    monkeypatch.setattr(tenders, "tender_sheet", lambda code: (tender, reqs) if code == "1001" else (None, None))

    leader = company(account_factory, "a@example.test", "Líder SAC", amount="90000")
    partner = company(account_factory, "b@example.test", "Socia SAC", amount="70000")
    assert "Adquisición de diésel B5" in leader.get("/consorcio?convocatoria=1001").text
    created = leader.post("/consorcio", data={"name": "Consorcio Diésel", "convocatoria": "1001"})
    assert created.location == "/consorcio/1?convocatoria=1001"
    leader.post("/consorcio/1/invitar", data={"email": "b@example.test"})
    partner.post("/consorcio/invitaciones/1/aceptar")

    html = leader.get("/consorcio/1?convocatoria=1001").text
    assert "Las bases piden un monto facturado de <strong>S/ 150,000.00</strong> (página 37)" in html
    assert 'value="150000.00"' in html
    assert "No cumple" in result_cell(html, "alone") and "S/ 160,000.00" in html
    assert "No cumple" not in result_cell(html, "consortium")
    # El monto escrito por el usuario reemplaza al de las bases; un código desconocido se ignora
    assert 'value="200000"' in leader.get("/consorcio/1?convocatoria=1001&facturado=200000").text
    assert leader.get("/consorcio/1?convocatoria=9999").status_code == 200


def test_la_ficha_de_la_convocatoria_ofrece_evaluarla_con_un_consorcio():
    from pathlib import Path
    template = (Path(__file__).resolve().parent.parent / "templates" / "tender.html").read_text(encoding="utf-8")
    assert "url_for('consortium.index', convocatoria=tender.code)" in template