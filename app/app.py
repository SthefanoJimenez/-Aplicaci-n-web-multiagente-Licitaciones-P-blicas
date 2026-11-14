from datetime import date, datetime, timezone
import re

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, stream_with_context, url_for
from sqlalchemy.exc import SQLAlchemyError

import config
import orchestrator
import rules
import auth
import documents
import consortium
from agents import classifier, evaluator, writer
from services import analytics, bases_download, charts, data, llm, schedule, tenders
from services import accounts, company_documents, eligibility, eligibility_chat, eligibility_store
from services.formatting import format_amount, format_pen, format_pen_total, format_quantity, markdown, unit_text
from services.chat_formatting import render_assistant_text


app = Flask(__name__)
app.secret_key = config.SECRET_KEY
auth.init_app(app)
documents.init_app(app)
consortium.init_app(app)


@app.context_processor
def common_context():
    return {"app_name": config.APP_NAME, "max_length": config.MAX_DESCRIPTION_LENGTH}


app.template_filter("pen")(format_pen)
app.template_filter("pen_total")(format_pen_total)
app.template_filter("quantity")(format_quantity)
app.template_filter("amount")(format_amount)
app.template_filter("unit_text")(unit_text)
app.template_filter("markdown")(markdown)
app.template_filter("place")(analytics.place_label)
app.template_filter("assistant_text")(render_assistant_text)


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
@app.get("/evaluar/mi-producto")
def evaluate_company_product():
    """Entrada con el perfil guardado y recuperación del último bien confirmado."""
    try:
        factory = auth.store()
        profile = accounts.get_profile(factory, g.current_user["id"])
        code = accounts.get_company_product(factory, g.current_user["id"])
    except SQLAlchemyError:
        return render_template("auth/unavailable.html"), 503
    if code in data.VALID_CODES and not request.args.get("cambiar"):
        session["code"] = code
        session["description"] = data.GOODS[code]["name"]
        session["candidates"] = [code]
        return redirect(url_for("market"))

    matches = classifier.profile_candidates(profile.get("business_activity", ""), profile.get("rubros", ""))
    session.pop("code", None)
    # Solo información pública del catálogo y una etiqueta genérica pasan a la cookie.
    session["description"] = "Bienes del perfil de la empresa"
    session["candidates"] = [item["code"] for item in matches]
    description = (profile.get("rubros") or profile.get("business_activity") or "")[:config.MAX_DESCRIPTION_LENGTH]
    return render_template("company_product.html", profile=profile, candidates=matches,
                           description=description, n_awards=data.N_AWARDS)


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
    except (llm.ModelError, ValueError) as e:
        return render_template("error.html", description=description.strip(), code=getattr(e, "code", 503)), 503

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


@app.route("/candidatos/directo", methods=["GET", "POST"])
def candidates_direct():
    """Búsqueda directa en catálogo usando TF-IDF cuando no hay API Key de Gemini configurada."""
    description = (request.values.get("description") or session.get("description") or "").strip()
    if not description:
        return redirect(url_for("describe"))
    preselected = classifier.preselect(description)
    valid, _ = classifier.validate(preselected)
    if not valid:
        session["description"] = description
        session.pop("candidates", None)
        return render_template("result.html", description=description, min_catalog=rules.MIN_CATALOG)
    candidates_list = [data.GOODS[c] for c in valid]
    session["description"] = description
    session.pop("code", None)
    session["candidates"] = [c["code"] for c in candidates_list]
    return render_template(
        "candidates.html",
        description=description,
        candidates=candidates_list,
        n_awards=data.N_AWARDS,
    )


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
    try:
        accounts.remember_company_product(auth.store(), g.current_user["id"], code)
    except SQLAlchemyError:
        return render_template("auth/unavailable.html"), 503
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
    
    # Si el código elegido pertenece a un rubro piloto (ej. 1510 combustibles o 2510 vehículos)
    pilot_rubro = None
    item_name = (data.GOODS.get(m["code"]) or {}).get("name", "")
    for code, label, pattern in [
        ("1510", "Combustibles", r"\b(diesel|gasolina|gasohol|glp|petroleo)\b"),
        ("2510", "Vehículos", r"\b(camioneta|automovil|vehiculo|volquete|camion|motocicleta)\b"),
    ]:
        if re.search(pattern, item_name, re.I):
            pilot_rubro = {"code": code, "name": label}
            break

    return render_template("verdict.html", m=m, e=evaluation, w=explanation, rnp=rnp,
                           d=response["detail"], below=below, pilot_rubro=pilot_rubro)


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
# Convocatorias vigentes (EN-007, HU-007)
# ---------------------------------------------------------------------------
PILOT_RUBRO_OPTIONS = [
    ("1510", "1510 · Combustibles y lubricantes"),
    ("2510", "2510 · Vehículos de motor"),
]


@app.get("/convocatorias")
def tenders_list():
    """Pantalla de convocatorias vigentes de bienes en los rubros piloto y los solicitados (EN-011)."""
    rubro = request.args.get("rubro", "").strip() or None
    department = request.args.get("departamento", "").strip().upper() or None
    request_notice = request.args.get("solicitud")        # "ok" o "invalida", tras «Solicitar este rubro»
    method = request.args.get("metodo", "").strip() or None

    tenders_state = tenders.availability()
    items = []
    rubro_info = None
    if tenders_state["ok"] and rubro and rubro != "sin":
        try:
            rubro_info = tenders.rubro_status(rubro)       # HU-013: ¿tiene datos? ¿se puede solicitar?
        except tenders.TendersUnavailable:
            rubro_info = None
    if tenders_state["ok"]:
        try:
            raw_tenders = tenders.open_tenders(rubro=rubro, method=method, department=department)
            now_utc = datetime.now(timezone.utc)
            for t in raw_tenders:
                item_dict = {
                    "ocid": t.ocid,
                    "code": t.code,
                    "title": t.title or t.description,
                    "entity": t.entity,
                    "department": t.department,
                    "method": t.method,
                    "rubros": t.rubros,
                    "published_at": t.published_at,
                    "deadline_at": t.deadline_at,
                    "days_since": tenders.days_since(t.published_at, now_utc),
                    "estimated_value": t.estimated_value,
                    "seace_url": tenders.seace_url(t),
                    "bases_label": bases_download.declared_label(tenders.bases_format(t)),
                    "catalog_code": tenders.match_catalog_item(t.title or t.description),
                }
                items.append(item_dict)
        except tenders.TendersUnavailable:
            pass

    # Métodos y departamentos disponibles para los filtros
    total_items = len(items)
    page_number = max(1, request.args.get("pagina", 1, type=int) or 1)
    items = items[(page_number - 1) * 50:page_number * 50]
    eligibility_results, eligibility_error = {}, None
    if g.get("current_user") and items:
        try:
            eligibility_results = eligibility.evaluate_many(auth.store(), g.current_user["id"], [t["code"] for t in items if t["code"]])
        except (SQLAlchemyError, tenders.TendersUnavailable, ValueError):
            eligibility_error = "No se pudo consultar la elegibilidad de su empresa; intente de nuevo."
    method_options = [
        "Subasta Inversa Electrónica",
        "Licitación Pública Abreviada",
        "Licitación Pública",
        "Adjudicación Simplificada",
        "Comparación de Precios",
    ]

    return render_template(
        "tenders.html",
        tenders=items,
        eligibility_results=eligibility_results, eligibility_error=eligibility_error,
        total_items=total_items, page_number=page_number, total_pages=max(1, (total_items + 49) // 50),
        tenders_state=tenders_state,
        pilot_rubros=tenders.rubro_options(PILOT_RUBRO_OPTIONS),   # catálogo completo de rubros (HU-013)
        rubro_info=rubro_info,
        request_notice=request_notice,
        departments=analytics.DEPARTMENTS,
        methods=method_options,
        f={"rubro": rubro or "", "department": department or "", "method": method or ""},
    )


@app.post("/convocatorias/solicitar-rubro")
def request_rubro():
    """«Solicitar este rubro» (HU-013): guarda la solicitud; la siguiente ingesta descarga sus convocatorias."""
    from services import rubros
    code = request.form.get("rubro", "")
    company = request.form.get("empresa", "").strip()[:200]
    try:
        saved = tenders.request_rubro(code, company)
    except rubros.InvalidRubro:
        return redirect(url_for("tenders_list", solicitud="invalida"), code=303)
    except tenders.TendersUnavailable:
        return redirect(url_for("tenders_list", rubro=code, solicitud="sin_conexion"), code=303)
    return redirect(url_for("tenders_list", rubro=saved, solicitud="ok"), code=303)


@app.get("/rubros")
def rubro_catalog():
    """Catálogo de rubros (HU-013): todas las familias UNSPSC con convocatorias de bienes del último mes."""
    try:
        rows = tenders.rubro_catalog()
        state = {"ok": True}
    except tenders.TendersUnavailable:
        rows, state = [], {"ok": False}
    query = request.args.get("q", "").strip()
    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in r["code"] or needle in (r["name"] or "").lower()]
    return render_template("rubros.html", rows=rows, state=state, q=query)


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

@app.get("/convocatorias/<code>")
def tender_sheet(code):
    """Ficha de una convocatoria (HU-010): sus datos y los requisitos de calificación de sus bases."""
    try:
        tender, reqs = tenders.tender_sheet(code)
    except tenders.TendersUnavailable:
        return render_template("tender.html", tender=None, reqs=None, unavailable=True, code=code), 503
    if tender is None:
        abort(404)
    rubro_names = tenders.rubro_names()
    assessment, saved_inputs, owned_documents, eligibility_error = None, {}, [], None
    if g.get("current_user"):
        try:
            factory, user_id = auth.store(), g.current_user["id"]
            assessment = eligibility.evaluate_many(factory, user_id, [code]).get(code)
            saved_inputs = eligibility_store.load_inputs(factory, user_id, [code]).get(code, {})
            owned_documents = company_documents.list_documents(factory, user_id)
        except (SQLAlchemyError, tenders.TendersUnavailable, ValueError):
            eligibility_error = "No se pudo consultar la evaluación privada; sus datos guardados se conservan. Intente nuevamente."
    return render_template(
        "tender.html", tender=tender, reqs=reqs, unavailable=False, code=code,
        seace_url=tenders.seace_url(tender),
        bases_label=bases_download.declared_label(tenders.bases_format(tender)),
        days_since=tenders.days_since(tender.published_at, datetime.now(timezone.utc)),
        rubros=[rubro_names.get(r, r) for r in tender.rubros],
        catalog_code=tenders.match_catalog_item(tender.title or tender.description),
        timeline=schedule.view(tender),
        assessment=assessment, saved_inputs=saved_inputs, owned_documents=owned_documents,
        eligibility_error=eligibility_error, review_options=eligibility.REVIEW_OPTIONS,
    )


@app.post("/elegibilidad/<code>")
def eligibility_save(code):
    """Guarda solo declaraciones propias; los requisitos y la empresa se resuelven en el servidor."""
    try:
        context = eligibility.load_tenders([code]).get(code)
        if context is None:
            abort(404)
        documents = company_documents.list_documents(auth.store(), g.current_user["id"])
        values, errors = eligibility.parse_form(request.form, [r["key"] for r in context["requirements"]], documents)
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            eligibility_store.save_inputs(auth.store(), g.current_user["id"], code, values)
            flash("Datos de evaluación guardados. El resultado se recalculó con su perfil y documentos actuales.", "success")
    except (SQLAlchemyError, tenders.TendersUnavailable):
        flash("No se pudo guardar la evaluación en este momento. Intente nuevamente.", "error")
    return redirect(url_for("tender_sheet", code=code, _anchor="elegibilidad"), code=303)


@app.get("/convocatorias/<code>/bases")
def tender_bases(code):
    """Descarga de las bases a través de la app: con el formato real y la extensión correcta.

    El enlace del SEACE entrega application/octet-stream sin un nombre confiable y el navegador
    guardaba el archivo con una extensión equivocada (ZIP "corrupto"). Ver services/bases_download.py.
    """
    try:
        tender = tenders.tender_by_code(code)
    except tenders.TendersUnavailable:
        return render_template("bases_unavailable.html", tender=None, url=None,
                               reason="la base de convocatorias no respondió"), 503
    url = tenders.seace_url(tender) if tender else None
    if not url:
        abort(404)
    try:
        bases = bases_download.open_bases(url)
    except bases_download.BasesUnavailable as e:
        return render_template("bases_unavailable.html", tender=tender, url=url, reason=e.reason), 502
    safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", str(tender.code or code))
    headers = {"Content-Disposition": f'attachment; filename="bases_{safe_code}.{bases.extension}"',
               "Cache-Control": "no-store"}
    if bases.size:
        headers["Content-Length"] = str(bases.size)
    return Response(stream_with_context(bases.chunks), mimetype=bases.mimetype, headers=headers)



# ---------------------------------------------------------------------------
# Integración del motor de elegibilidad HU-010 con el asistente HU-014
# ---------------------------------------------------------------------------
# La conversación privada vive en la base de cuentas; la cookie conserva solo los tokens de sesión.
ASSISTANT_MAX_QUERY = 1000


@app.get("/asistente")
def assistant_chat():
    """Pantalla del asistente con conversación privada y perfil empresarial persistente."""
    factory, user_id = auth.store(), g.current_user["id"]
    history, profile = eligibility_store.load_chat(factory, user_id)
    legacy_history, legacy_profile = session.pop("assistant_history", []), session.pop("supplier_profile", {})
    if legacy_history and not history:
        eligibility_store.save_chat(factory, user_id, legacy_history, legacy_profile)
        history, profile = eligibility_store.load_chat(factory, user_id)
    profile = eligibility_chat.sidebar(accounts.get_profile(factory, user_id), profile)
    selected_code = request.args.get("convocatoria", "").strip()
    if selected_code:
        if not tenders.tender_by_code(selected_code):
            abort(404)
        profile["selected_tender_code"] = selected_code
        eligibility_store.save_chat(factory, user_id, history, profile)
    tenders_state = tenders.availability()
    return render_template(
        "assistant.html",
        history=history,
        profile=profile,
        tenders_state=tenders_state,
        active_nav="assistant",
    )


@app.post("/asistente")
def assistant_ask():
    """Consulta autenticada al mismo motor de reglas que usa la ficha de elegibilidad."""
    query = request.form.get("query", "").strip()[:ASSISTANT_MAX_QUERY]
    if not query:
        return redirect(url_for("assistant_chat"))

    factory, user_id = auth.store(), g.current_user["id"]
    history, profile = eligibility_store.load_chat(factory, user_id)

    try:
        result = eligibility_chat.answer(query, factory, user_id, history=history, profile=profile)
    except Exception:
        # La conversación nunca debe romper la pantalla: se avisa y se conserva el historial.
        app.logger.error("No se pudo completar la consulta del asistente")
        flash("No se pudo completar la consulta en este momento. Vuelva a intentarlo en unos segundos.", "error")
        return redirect(url_for("assistant_chat"))

    # Historial privado en la base de cuentas: no cabe completo ni debe viajar en la cookie.
    eligibility_store.save_chat(factory, user_id,
        history + [
            {"role": "user", "content": query},
            {"role": "assistant", "content": result["response"]},
        ], result["profile"]
    )
    session.pop("assistant_history", None)
    session.pop("supplier_profile", None)

    return redirect(url_for("assistant_chat"))


@app.post("/asistente/limpiar")
def assistant_clear():
    """Reinicia solo la conversación; conserva perfil, documentos y declaraciones de evaluación."""
    session.pop("assistant_history", None)
    session.pop("supplier_profile", None)
    eligibility_store.clear_chat(auth.store(), g.current_user["id"])
    return redirect(url_for("assistant_chat"))


if __name__ == "__main__":
    app.run(debug=True)
