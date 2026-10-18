"""Analítica sobre los datos planos: panorama, detalle del mercado y preparación de datos."""
import pytest

from agents import classifier
from app import app
from services import analytics, data, llm

CEMENT_I = 12771


@pytest.fixture
def no_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("La analítica no debe usar el modelo de lenguaje")
    monkeypatch.setattr(llm, "_call_gemini", forbidden)
    monkeypatch.setattr(llm, "generate_json", forbidden)
    monkeypatch.setattr(classifier, "classify", forbidden)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def test_detail_has_same_rows_as_analysis_table():
    """awards_detail.csv es la tabla completa de EN-003; awards.csv es su parte del catálogo."""
    assert data.CLEANING_FUNNEL["filas"].iloc[-1] == len(data.AWARDS_DETAIL)
    in_catalog = data.AWARDS_DETAIL["codigoitem"].isin(data.VALID_CODES)
    assert in_catalog.sum() == len(data.AWARDS)


def test_natural_persons_are_not_named():
    suppliers = data.AWARDS_DETAIL["proveedor"].dropna()
    assert (suppliers == "Persona natural").any()
    detail = analytics.good_detail(CEMENT_I, data.GOODS[CEMENT_I]["unit"])
    assert all(s["name"] != "Persona natural" for s in detail["suppliers"])


def test_overview_totals_match_filters():
    total = analytics.overview(None, None, None, None)
    assert total["n_awards"] == len(data.AWARDS_DETAIL)
    assert sum(b["value"] for b in total["by_size"]) == total["n_awards"]
    assert sum(y["value"] for y in total["by_year"]) == total["n_awards"]
    local = analytics.overview(None, "LA LIBERTAD", None, None)
    assert 0 < local["n_awards"] < total["n_awards"]
    assert all(p["label"] == "LA LIBERTAD" for p in local["by_place"])


def test_clean_filters_ignores_unknown_values():
    assert analytics.clean_filters("1999", "MARTE", "<script>", "gigante", "OTRO", "13") == (None,) * 6
    year = analytics.BP_YEARS[0]
    assert analytics.clean_filters(str(year), None, None, "hasta-50k", "GOBIERNO LOCAL", "3") == (
        year, None, None, "hasta-50k", "GOBIERNO LOCAL", 3)


def test_good_detail_is_consistent():
    d = analytics.good_detail(CEMENT_I, data.GOODS[CEMENT_I]["unit"])
    assert sum(p["n"] for p in d["places"]) == d["n_awards"]
    assert sum(b["value"] for b in d["by_size"]) == d["n_awards"]
    assert len(d["recent"]) <= 12
    dates = [r["date"] for r in d["recent"]]
    assert dates == sorted(dates, reverse=True)


def test_market_screen_shows_detail(client, no_model):
    with client.session_transaction() as session:
        session["code"] = CEMENT_I
        session["candidates"] = [CEMENT_I]
    html = client.get("/mercado").get_data(as_text=True)
    for text in ("¿Dónde se compra?", "¿Quiénes compran?", "¿Quiénes ganan?", "Últimas adjudicaciones", "Ingresar mi costo"):
        assert text in html


def test_overview_and_data_screens(client, no_model):
    assert "Panorama del mercado" in client.get("/panorama").get_data(as_text=True)
    filtered = client.get("/panorama?anio=2024&lugar=LA+LIBERTAD").get_data(as_text=True)
    assert "Mostrando: 2024, La Libertad" in filtered
    html = client.get("/datos").get_data(as_text=True)
    assert "60,024" in html and "199,985" in html


def test_good_sheet_is_separate_from_the_evaluation_flow(client, no_model):
    """La ficha del panorama no debe escribir en la sesión: si no, el paso 2 mostraría
    como «descripción del usuario» un bien que el usuario nunca escribió."""
    with client.session_transaction() as session:
        session["description"] = "cemento en bolsa"
        session["candidates"] = [12953]
    html = client.get(f"/panorama/bien/{CEMENT_I}").get_data(as_text=True)
    assert "Ficha del bien" in html and "Verifique estos datos" in html
    assert "Ingresar mi costo" not in html
    with client.session_transaction() as session:
        assert session["description"] == "cemento en bolsa"
        assert session["candidates"] == [12953]
        assert "code" not in session
    assert client.get("/bien/12771").status_code == 404
    assert client.get("/panorama/bien/999999").headers["Location"].endswith("/panorama")


def test_downloads_have_every_matching_award(client, no_model):
    response = client.get(f"/panorama/bien/{CEMENT_I}.csv")
    lines = response.get_data(as_text=True).lstrip("\ufeff").splitlines()
    assert response.mimetype == "text/csv"
    assert lines[0].startswith("año,fecha_buena_pro,nomenclatura_proceso,codigo_convocatoria")
    assert len(lines) - 1 == int((data.AWARDS["codigoitem"] == CEMENT_I).sum())
    local = client.get("/panorama/datos.csv?lugar=LA+LIBERTAD").get_data(as_text=True)
    assert len(local.strip().splitlines()) - 1 == analytics.overview(None, "LA LIBERTAD")["n_awards"]


def test_panorama_has_monthly_chart_and_map(client, no_model):
    o = analytics.overview()
    assert sum(m["n"] for m in o["by_month"]) + o["n_without_date"] == o["n_awards"]
    assert set(o["map"]["departments"]) <= set(analytics.DEPARTMENTS)
    html = client.get("/panorama").get_data(as_text=True)
    assert "¿Cuándo se compra?" in html and 'class="hit"' in html
    assert html.count("<path d=") >= 25, "un polígono por departamento"
    assert "/panorama?lugar=LA+LIBERTAD" in html or "/panorama?lugar=LA%20LIBERTAD" in html


def test_each_award_keeps_its_own_supplier():
    """Un ítem adjudicado a varios proveedores no debe repetir el nombre del primero."""
    d = data.AWARDS_DETAIL
    key = d["codigoconvocatoria"].astype(str) + "|" + d["n_item"].astype(str)
    repeated = d[d.duplicated(keep=False)]
    assert (repeated["proveedor"] == "Persona natural").all(), "solo se parecen las filas de personas naturales"
    multi = d[key.duplicated(keep=False) & (d["proveedor"] != "Persona natural")]
    assert not multi.duplicated(["codigoconvocatoria", "n_item", "proveedor", "cantidad", "monto_adjudicado"]).any()


def test_entity_type_breakdown_and_filter(client, no_model):
    total = analytics.overview()
    assert sum(t["n"] for t in total["by_entity_type"]) == total["n_awards"]
    local_gov = analytics.overview(None, None, None, None, "GOBIERNO LOCAL")
    assert [t["key"] for t in local_gov["by_entity_type"]] == ["GOBIERNO LOCAL"]
    html = client.get("/panorama?entidad=GOBIERNO+LOCAL").get_data(as_text=True)
    assert "¿Qué tipo de entidad compra?" in html and "Gobierno local (municipalidades)" in html
    detail = analytics.good_detail(CEMENT_I, data.GOODS[CEMENT_I]["unit"])
    assert sum(t["n"] for t in detail["entity_types"]) == detail["n_awards"]


def test_home_page_uses_all_data(client, no_model):
    html = client.get("/").get_data(as_text=True)
    assert "sin muestreo" in html
    assert f"{len(data.AWARDS_DETAIL):,}" in html
    assert 'href="/evaluar"' in html
    s = analytics.landing()
    assert s["example"]["n_awards"] == int((data.AWARDS["codigoitem"] == CEMENT_I).sum())


def test_market_renders_when_province_is_missing(client, no_model):
    """Regresión: un vacío de pandas (NA) en la provincia rompía la tabla de últimas adjudicaciones."""
    d = analytics.DETAIL
    candidates = d.loc[d["provincia_entrega"].isna() & d["codigoitem"].isin(data.VALID_CODES), "codigoitem"]
    for code in sorted(set(candidates))[:40]:
        detail = analytics.good_detail(code, data.GOODS[code]["unit"])
        if any(r["province"] is None for r in detail["recent"]):
            break
    else:
        pytest.skip("ningún bien del catálogo tiene provincia vacía entre sus últimas compras")
    with client.session_transaction() as session:
        session["code"] = code
        session["candidates"] = [code]
    assert client.get("/mercado").status_code == 200


def test_month_filter_uses_buena_pro_date(client, no_model):
    march = analytics.overview(None, None, None, None, None, 3)
    expected = int((analytics.DETAIL["fecha_buenapro"].str.slice(5, 7) == "03").sum())
    assert march["n_awards"] == expected
    html = client.get("/panorama?mes=3&anio=2024").get_data(as_text=True)
    assert "Mostrando: Marzo, 2024" in html
    assert '<option value="">Todos</option>' in html


def test_landing_has_a_single_evaluate_button_and_new_footer(client, no_model):
    html = client.get("/").get_data(as_text=True)
    body = html.split("</header>", 1)[1]
    assert body.count(">Evaluar mi producto<") == 1
    assert "Cómo leer el resultado" not in html
    assert "data-carousel" in html and "data-tour-start" in html
    assert "© 2026 LicitaFácil. Todos los derechos reservados." in html
    assert "Taller Integrador" not in html


def test_large_purchase_markets_are_flagged():
    big = next(c for c in sorted(data.VALID_CODES)
               if analytics.good_detail(c, data.GOODS[c]["unit"])["large"]["is_large"])
    detail = analytics.good_detail(big, data.GOODS[big]["unit"])
    assert detail["median_amount"] >= analytics.HIGH_AMOUNT_THRESHOLD
    assert not analytics.good_detail(CEMENT_I, data.GOODS[CEMENT_I]["unit"])["large"]["is_large"]


def test_multidepartmental_is_explained_and_not_placed_in_one_department(client, no_model):
    html = client.get("/panorama?lugar=MULTIDEPARTAMENTAL").get_data(as_text=True)
    assert "¿Qué significa «multidepartamental»?" in html
    assert "url(#hatch)" in html and "varios departamentos, sin detalle" in html
    assert "Sede de la entidad" in html
    overview = client.get("/panorama").get_data(as_text=True)
    assert "No aparecen en el mapa porque no tienen un solo departamento" in overview


def test_exterior_lists_each_purchase_without_inventing_a_country(client, no_model):
    o = analytics.overview(None, "EXTERIOR")
    html = client.get("/panorama?lugar=EXTERIOR").get_data(as_text=True)
    assert "¿Qué significa «exterior»?" in html and "no publica el país" in html
    assert "<path d=" not in html.split("places-title", 1)[1].split("</section>", 1)[0]
    assert len(o["special"]["exterior_rows"]) == o["n_awards"]