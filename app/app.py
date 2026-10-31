from datetime import date

from flask import Flask, Response, redirect, render_template, request, session, url_for

import config
import orchestrator
import rules
from agents import evaluator, writer
from services import analytics, charts, data, llm, tenders
from services.formatting import format_amount, format_pen, unit_text


app = Flask(__name__)
app.secret_key = config.SECRET_KEY


@app.context_processor
def common_context():
    return {"app_name": config.APP_NAME, "max_length": config.MAX_DESCRIPTION_LENGTH}


app.template_filter("pen")(format_pen)
app.template_filter("amount")(format_amount)
app.template_filter("unit_text")(unit_text)
app.template_filter("place")(analytics.place_label)


@app.template_filter("number")
def format_number(value):
    """Entero si no tiene decimales (5); si los tiene, con uno (2.5)."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


@app.template_filter("percent")
def format_percent(value):
    """Porcentaje con un decimal: 0.4%, 23.2%."""
    return f"{value:.1f}%"


MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "set", "oct", "nov", "dic"]


@app.template_filter("date_pe")
def format_date(value):
    """2025-03-14 -> 14 mar 2025."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return "—"
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


# ---------------------------------------------------------------------------
# Página de inicio
# ---------------------------------------------------------------------------
@app.get("/")
def home():
    """Portada: qué hace la aplicación, con cifras y un ejemplo real calculados con todos los datos."""
    s = analytics.landing()
    example = s["example"]
    evaluation = evaluator.evaluate(example["cost"], example) if example else None
    return render_template("home.html", s=s, ex=example, e=evaluation, rules=rules)


# ---------------------------------------------------------------------------
# Flujo de evaluación: 5 pasos
# ---------------------------------------------------------------------------
@app.get("/evaluar")
def describe():
    """Paso 1: descripción del bien."""
    retry = bool(request.args.get("reintentar"))
    return render_template(
        "describe.html",
        description=session.get("description", "") if retry else "",
        retry=retry,
        stats={"n_awards": len(data.AWARDS_DETAIL), "n_catalog": len(data.VALID_CODES)},
    )


def _describe_page(description, error, status):
    stats = {"n_awards": len(data.AWARDS_DETAIL), "n_catalog": len(data.VALID_CODES)}
    return render_template("describe.html", description=description, error=error, stats=stats), status


@app.post("/candidatos")
def candidates():
    """Paso 2: selección del código, o resultado sin evidencia."""
    description = request.form.get("description", "")
    try:
        response = orchestrator.identify_good(description)
    except orchestrator.InvalidDescription as e:
        return _describe_page(description, str(e), 400)
    except llm.ModelError as e:
        return render_template("error.html", description=description.strip(), code=getattr(e, "code", None)), 503

    session["description"] = description.strip()
    session.pop("code", None)
    if response["result"] == orchestrator.NO_EVIDENCE:
        session.pop("candidates", None)
        return render_template(
            "result.html", description=description.strip(), min_catalog=rules.MIN_CATALOG
        )

    session["candidates"] = [c["code"] for c in response["candidates"]]
    return render_template("candidates.html", description=description.strip(),
                           candidates=response["candidates"], n_awards=data.N_AWARDS)


@app.get("/candidatos")
def back_to_candidates():
    """Vuelve al paso 2 con los candidatos ya encontrados, sin llamar otra vez al modelo."""
    codes = [c for c in session.get("candidates", []) if c in data.GOODS]
    if not codes:
        return redirect(url_for("describe"))
    return render_template(
        "candidates.html",
        description=session.get("description", ""),
        candidates=[data.GOODS[c] for c in codes],
        selected=session.get("code") if session.get("code") in codes else None,
        n_awards=data.N_AWARDS,
    )


@app.post("/confirmar")
def confirm():
    """Guarda el código confirmado. Solo acepta uno de los candidatos ofrecidos."""
    code = request.form.get("code", type=int)
    if code not in session.get("candidates", []) or code not in data.VALID_CODES:
        return redirect(url_for("describe"))
    session["code"] = code
    return redirect(url_for("market"))


@app.get("/mercado")
def market():
    """Paso 3: rango de precios (HU-002), competencia (HU-003) y detalle del mercado (criterios ampliados)."""
    code = session.get("code")
    if code not in data.GOODS:
        return redirect(url_for("describe"))
    response = orchestrator.analyze_market(code)
    if response["result"] == orchestrator.NO_EVIDENCE:
        return render_template(
            "result.html", description=data.GOODS[code]["name"], min_catalog=rules.MIN_CATALOG
        )
    return render_template(
        "market.html",
        m=response["market"],
        d=response["detail"],
        has_evidence=response["result"] == orchestrator.RANGE_WITH_EVIDENCE,
        min_verdict=rules.MIN_VERDICT, rules=rules,
    )


def _market_with_evidence():
    """Mercado del bien confirmado, solo si tiene evidencia para un veredicto; si no, None."""
    code = session.get("code")
    if code not in data.GOODS:
        return None
    response = orchestrator.analyze_market(code)
    if response["result"] != orchestrator.RANGE_WITH_EVIDENCE:
        return None
    return response


@app.get("/costo")
def cost():
    """Paso 4: el usuario ingresa su costo unitario."""
    response = _market_with_evidence()
    if response is None:
        return redirect(url_for("market") if session.get("code") in data.GOODS else url_for("describe"))
    return render_template("cost.html", m=response["market"])


RNP_ANSWERS = ("si", "no", "no-se")


@app.post("/veredicto")
def verdict():
    """Paso 5: veredicto y explicación. El costo y la respuesta sobre el RNP solo viven en
    esta petición: no se guardan en la sesión, no se envían al modelo de lenguaje y no se
    registran en LangSmith."""
    response = _market_with_evidence()
    if response is None:
        return redirect(url_for("describe"))
    cost_text = request.form.get("cost", "")
    rnp = request.form.get("rnp") if request.form.get("rnp") in RNP_ANSWERS else None
    try:
        cost_value = evaluator.parse_cost(cost_text)
    except evaluator.InvalidCost as e:
        return render_template("cost.html", m=response["market"], error=str(e), cost_text=cost_text, rnp=rnp), 400
    m = response["market"]
    evaluation = evaluator.evaluate(cost_value, m)
    explanation = writer.explain(evaluation, m, response["detail"])
    below = analytics.share_below(m["code"], m["unit"], cost_value)
    return render_template("verdict.html", m=m, e=evaluation, w=explanation, rnp=rnp,
                           d=response["detail"], below=below)


@app.get("/veredicto")
def verdict_without_cost():
    return redirect(url_for("cost"))


# ---------------------------------------------------------------------------
# Analítica de los datos planos
# ---------------------------------------------------------------------------
def _panorama_filters():
    return analytics.clean_filters(
        request.args.get("anio"), request.args.get("lugar"),
        request.args.get("proceso"), request.args.get("tamanio"), request.args.get("entidad"),
        request.args.get("mes"),
    )


FILTER_KEYS = ("anio", "lugar", "proceso", "tamanio", "entidad", "mes")


def _csv(table, filename):
    """CSV con BOM para que Excel muestre bien las tildes."""
    body = "\ufeff" + table.to_csv(index=False)
    return Response(body, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/panorama")
def overview():
    """Panorama del mercado de bienes del Estado, con filtros, gráficos y mapa."""
    filters = _panorama_filters()
    o = analytics.overview(*filters)
    query = {k: v for k, v in zip(FILTER_KEYS, filters) if v}
    return render_template(
        "overview.html",
        o=o, chart=charts.area_chart(o.get("by_month", [])), geo=charts.peru_map(), query=query,
        f=dict(zip(("year", "department", "process", "size", "entity_type", "month"), filters)),
        years=analytics.BP_YEARS, months=analytics.MONTH_NAMES, departments=analytics.DEPARTMENTS,
        processes=analytics.PROCESS_OPTIONS, sizes=analytics.SIZE_BUCKETS,
        size_labels=analytics.SIZE_LABELS, entity_types=analytics.ENTITY_TYPE_OPTIONS,
        entity_type_labels=dict(analytics.ENTITY_TYPE_OPTIONS),
    )


@app.get("/panorama/datos.csv")
def overview_csv():
    """Las adjudicaciones que cumplen los filtros del panorama, para verificarlas una por una."""
    return _csv(analytics.download_rows(analytics.filtered(*_panorama_filters())), "licitafacil_adjudicaciones.csv")


@app.get("/panorama/bien/<int:code>")
def good_sheet(code):
    """Ficha de consulta de un bien del catálogo. Es independiente del flujo de evaluación:
    no usa ni modifica la sesión, así que no aparece como si el usuario lo hubiera buscado."""
    if code not in data.VALID_CODES:
        return redirect(url_for("overview"))
    response = orchestrator.analyze_market(code)
    if response["result"] == orchestrator.NO_EVIDENCE:
        return redirect(url_for("overview"))
    return render_template(
        "good.html", m=response["market"], d=response["detail"],
        has_evidence=response["result"] == orchestrator.RANGE_WITH_EVIDENCE, min_verdict=rules.MIN_VERDICT,
    )


@app.get("/panorama/bien/<int:code>.csv")
def good_csv(code):
    if code not in data.VALID_CODES:
        return redirect(url_for("overview"))
    rows = analytics.download_rows(analytics.good_rows(code, data.GOODS[code]["unit"]))
    return _csv(rows, f"licitafacil_cubso_{code}.csv")


@app.get("/datos")
def data_preparation():
    """Cómo preparamos los datos: embudo de limpieza, reglas y calidad (EN-003, SP-002, DO-002)."""
    return render_template(
        "data.html",
        funnel=analytics.funnel(), quality=analytics.quality(), atypical=analytics.atypical_summary(),
        rules=rules,
    )

# ---------------------------------------------------------------------------
# Convocatorias vigentes (EN-007): estado de la base para el aviso de RN-002
# ---------------------------------------------------------------------------
@app.get("/convocatorias/estado")
def tenders_status():
    """Estado de la base de convocatorias en JSON: si responde, la última actualización y cuántas
    vigentes hay. Sirve para comprobar la conexión desde Render y medir el arranque en frío de Neon."""
    state = tenders.availability()
    body = {key: state[key] for key in ("ok", "reason", "stale", "last_failed")}
    body["last_update"] = state["last_update"].isoformat() if state["last_update"] else None
    if state["ok"]:
        try:
            body["open_tenders"] = len(tenders.open_tenders())
        except tenders.TendersUnavailable:
            body["ok"], body["reason"] = False, "sin_conexion"
    return body, (200 if body["ok"] else 503)

if __name__ == "__main__":
    app.run(debug=True)