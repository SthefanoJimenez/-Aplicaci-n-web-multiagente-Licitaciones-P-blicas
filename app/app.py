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
import staff_views
from agents import classifier, evaluator, writer
from services import analytics, bases_download, charts, data, llm, schedule, tenders
from services import accounts, company_documents, eligibility, eligibility_chat, eligibility_store
from services.formatting import format_amount, format_pen, format_pen_total, format_quantity, markdown, unit_text
from services.chat_formatting import render_assistant_text
from services.ocds_source import CATEGORY_LABELS


app = Flask(__name__)
app.secret_key = config.SECRET_KEY
auth.init_app(app)
documents.init_app(app)
consortium.init_app(app)
staff_views.init_app(app)


@app.get("/healthz")
def healthz():
    """Health check ultra-ligero para Render sin cargar consultas ni templates."""
    return "ok", 200


@app.context_processor
def common_context():
    return {"app_name": config.APP_NAME, "max_length": config.MAX_DESCRIPTION_LENGTH,
            "chat_history": _chat_state()[0] if g.get("current_user") else [],
            "data_period": data.PERIOD, "data_period_words": data.PERIOD_WORDS, "data_last_award": data.LAST_AWARD}


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
    """Fecha de calendario; los instantes se muestran en la zona de Lima."""
    try:
        if isinstance(value, datetime):
            value = tenders.as_utc(value).astimezone(tenders.LIMA).date()
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
    
    # Rubro de convocatorias vigentes al que pertenece el bien, con el catálogo completo de rubros (antes solo
    # combustibles y vehículos, escritos en el código). Si no se reconoce, no se ofrece el enlace.
    pilot_rubro = _rubro_of_good((data.GOODS.get(m["code"]) or {}).get("name", ""))

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
        request.args.get("mes"), request.args.get("bien"),
    )


FILTER_KEYS = ("anio", "lugar", "proceso", "tamanio", "entidad", "mes", "bien")


def _csv(table, filename):
    """CSV con BOM para que Excel muestre bien las tildes."""
    body = "\ufeff" + table.to_csv(index=False)
    return Response(body, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _rubro_of_good(item_name):
    """{"code", "name"} del rubro (familia UNSPSC) que corresponde al nombre del bien, o None."""
    from services import rubros
    try:
        names_map = tenders.rubro_names()
    except tenders.TendersUnavailable:
        return None
    found = rubros.match(item_name or "", names_map)
    return {"code": found[0]["code"], "name": found[0]["name"]} if found else None


def _profile_rubro():
    """Rubros que declaró la empresa en su perfil, para ofrecer «Ver el panorama de mi rubro» (o None)."""
    user = getattr(g, "current_user", None)
    if not user:
        return None
    try:
        from services import accounts
        rubros_text = (accounts.get_profile(auth.store(), user["id"]) or {}).get("rubros") or ""
    except Exception:          # sin base de cuentas: el panorama sigue funcionando
        return None
    first = re.split(r"[,;/\n]| y ", rubros_text)[0].strip()
    return first[:analytics.GOOD_MAX] if analytics.good_words(first) else None


@app.get("/panorama")
def overview():
    """Panorama del mercado de bienes del Estado, con filtros, gráficos y mapa."""
    filters = _panorama_filters()
    o = analytics.overview(*filters)
    query = {k: v for k, v in zip(FILTER_KEYS, filters) if v}
    return render_template(
        "overview.html",
        o=o, chart=charts.area_chart(o.get("by_month", [])), geo=charts.peru_map(), query=query,
        f=dict(zip(("year", "department", "process", "size", "entity_type", "month", "good"), filters)),
        my_rubro=_profile_rubro(),
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
    """Convocatorias vigentes de bienes, servicios y obras de los rubros de la ingesta (EN-011).

    para_mi=1 (con sesión): solo lo que ofrece la empresa, evaluado y ordenado por semáforo
    (services/matching.py, pedido del asesor del 19/11/2026).
    """
    from services import matching
    rubro = request.args.get("rubro", "").strip() or None
    department = request.args.get("departamento", "").strip().upper() or None
    request_notice = request.args.get("solicitud")        # "ok" o "invalida", tras «Solicitar este rubro»
    method = request.args.get("metodo", "").strip() or None
    category = request.args.get("categoria", "").strip().lower()
    category = category if category in CATEGORY_LABELS else None
    for_me = bool(g.get("current_user")) and request.args.get("para_mi") == "1"

    tenders_state = tenders.availability()
    items = []
    total_items = 0
    page_number = max(1, request.args.get("pagina", 1, type=int) or 1)
    rubro_info = None
    category_counts, methods = {}, []
    match = None
    if tenders_state["ok"] and rubro and rubro != "sin":
        try:
            rubro_info = tenders.rubro_status(rubro)       # HU-013: ¿tiene datos? ¿se puede solicitar?
        except tenders.TendersUnavailable:
            rubro_info = None
    if tenders_state["ok"]:
        try:
            raw_tenders = tenders.open_tenders(rubro=rubro, method=method, department=department, category=category)
            category_counts = tenders.category_counts()
            methods = tenders.method_options(category)
            if for_me:
                match = matching.candidates(auth.store(), g.current_user["id"], raw_tenders)
                raw_tenders = [t for t, _ in match["rows"]]
            total_items = len(raw_tenders)
            start = (page_number - 1) * 50
            now_utc = datetime.now(timezone.utc)
            for t in raw_tenders[start:start + 50]:
                item_dict = {
                    "ocid": t.ocid,
                    "code": t.code,
                    "title": t.title or t.description,
                    "entity": t.entity,
                    "department": t.department,
                    "method": t.method,
                    "category": t.category,
                    "category_label": CATEGORY_LABELS.get(t.category, ""),
                    "rubros": t.rubros,
                    "published_at": t.published_at,
                    "deadline_at": t.deadline_at,
                    "days_since": tenders.days_since(t.published_at, now_utc),
                    "estimated_value": t.estimated_value,
                    "seace_url": tenders.seace_url(t),
                    "bases_label": bases_download.declared_label(tenders.bases_format(t)),
                    # La ficha de precios históricos existe solo para bienes (precio unitario por CUBSO)
                    "catalog_code": tenders.match_catalog_item(t.title or t.description) if t.category == "bienes" else None,
                }
                items.append(item_dict)
        except (tenders.TendersUnavailable, SQLAlchemyError, ValueError):
            # Antes se ignoraba y la pantalla decía «0 convocatorias vigentes» (13/11/2026): ahora avisa
            tenders_state = dict(tenders_state, ok=False, reason="sin_conexion")
            match = None

    eligibility_results, eligibility_error = {}, None
    if match:
        eligibility_results = {str(t.code): r for t, r in match["rows"]}
    elif g.get("current_user") and items:
        try:
            eligibility_results = eligibility.evaluate_many(auth.store(), g.current_user["id"], [t["code"] for t in items if t["code"]])
        except (SQLAlchemyError, tenders.TendersUnavailable, ValueError):
            eligibility_error = "No se pudo consultar la elegibilidad de su empresa; intente de nuevo."

    return render_template(
        "tenders.html",
        tenders=items,
        eligibility_results=eligibility_results, eligibility_error=eligibility_error,
        total_items=total_items, page_number=page_number, total_pages=max(1, (total_items + 49) // 50),
        tenders_state=tenders_state,
        pilot_rubros=tenders.rubro_options(PILOT_RUBRO_OPTIONS, category or None),   # de más a menos vigentes
        rubro_info=rubro_info,
        request_notice=request_notice,
        departments=analytics.DEPARTMENTS,
        methods=methods,
        categories=CATEGORY_LABELS, category_counts=category_counts,
        match=match, for_me=for_me,
        f={"rubro": rubro or "", "department": department or "", "method": method or "",
           "category": category or "", "for_me": "1" if for_me else ""},
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
    """Catálogo de rubros (HU-013): familias UNSPSC con convocatorias de bienes, servicios y obras del último
    mes. Filtro «Objeto» (20/11/2026): solo los rubros con vigentes de bienes, servicios u obras."""
    category = request.args.get("categoria", "").strip().lower()
    category = category if category in CATEGORY_LABELS else ""
    try:
        rows = tenders.rubro_catalog(category or None)
        state = {"ok": True}
    except tenders.TendersUnavailable:
        rows, state = [], {"ok": False}
    query = request.args.get("q", "").strip()
    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in r["code"] or needle in (r["name"] or "").lower()]
    return render_template("rubros.html", rows=rows, state=state, q=query, category=category,
                           categories=CATEGORY_LABELS)


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
# Asistente de postulación (HU-010 / HU-014)
# ---------------------------------------------------------------------------
ASSISTANT_MAX_QUERY = 1000
# Preguntas por el listado («¿a cuáles puedo postular?», «¿qué otras convocatorias hay?»): no se atan a la
# convocatoria de la que se habló antes. Las de seguimiento («¿qué me falta?») sí la conservan (23/11/2026).
LIST_QUESTION = re.compile(r"\b(cu[aá]les|convocatorias|licitaciones|procesos|otras|todas|listado|lista)\b", re.I)


def _assistant_reply(text, status="ok", code=200, detail="", detail_count=0):
    """Respuesta JSON del widget: el Markdown se convierte a HTML seguro en el servidor.

    `text` es el resumen que se muestra en el chat; `detail` es el contenido completo que el
    widget abre en una ventana interna para no llenar la conversación.
    """
    payload = {"ok": code == 200, "status": status, "html": markdown(text) if text else ""}
    if detail:
        payload["detail"] = markdown(detail)
        payload["detail_count"] = detail_count
    return payload, code


def _chat_state():
    factory, user_id = auth.store(), g.current_user["id"]
    messages, context = eligibility_store.load_chat(factory, user_id)
    legacy = session.pop("assistant_history", [])
    legacy_rubros = session.pop("assistant_rubros", [])
    if legacy and not messages:
        messages = legacy
        context["rubros"] = legacy_rubros
        eligibility_store.save_chat(factory, user_id, messages, context)
    return messages, context


@app.post("/asistente/mensaje")
def assistant_message():
    """Una consulta del widget flotante. La respuesta es JSON para no recargar la página."""
    from services import assistant

    query = (request.form.get("query") or "").strip()[:ASSISTANT_MAX_QUERY]
    if not query:
        return _assistant_reply("", status="empty", code=400)
    history, chat_context = _chat_state()
    if LIST_QUESTION.search(query) and not eligibility_chat._code(query):
        chat_context.pop("selected_tender_code", None)
    if chat_context.get("selected_tender_code") and not eligibility_chat._code(query):
        query_for_engine = query + " · Convocatoria " + chat_context["selected_tender_code"]
    else:
        query_for_engine = query
    try:
        result = assistant.ask_assistant(
            query_for_engine,
            user_id=g.current_user["id"],
            factory=auth.store(),
            email=g.current_user["email"],
            history=history,
            remembered_rubros=chat_context.get("rubros", []),
        )
    except Exception:
        # El chat nunca debe romper la página: se avisa y se conserva el historial.
        app.logger.exception("Falló el asistente de postulación")
        return _assistant_reply(
            "No se pudo completar la consulta en este momento. Vuelva a intentarlo en unos segundos.",
            status="error", code=503)

    # El resumen va al chat y el detalle a la ventana interna del widget.
    summary, _, detail = result["response"].partition(assistant.DETAIL_MARKER)
    messages = history + [{"role": "user", "content": query},
                          {"role": "assistant", "content": summary + (assistant.DETAIL_MARKER + detail if detail else "")}]
    if result.get("rubros"):
        chat_context["rubros"] = result["rubros"][:3]
    explicit_code = eligibility_chat._code(query)
    if explicit_code:
        chat_context["selected_tender_code"] = explicit_code
    eligibility_store.save_chat(auth.store(), g.current_user["id"], messages, chat_context)
    return _assistant_reply(summary, status=result.get("status", "ok"),
                            detail=detail, detail_count=result.get("detail_count", 0))


@app.post("/asistente/limpiar")
def assistant_clear():
    """Reinicia la conversación del asistente y devuelve el panel vacío."""
    eligibility_store.clear_chat(auth.store(), g.current_user["id"])
    session.pop("assistant_history", None)
    session.pop("assistant_rubros", None)
    return {"ok": True}


@app.get("/asistente")
def assistant_chat():
    """La página anterior del asistente ya no existe: el chat vive en el widget de todas las pantallas."""
    code = request.args.get("convocatoria") or request.args.get("code")
    if code and re.fullmatch(r"[0-9]{4,10}", code):
        messages, context = _chat_state()
        context["selected_tender_code"] = code
        eligibility_store.save_chat(auth.store(), g.current_user["id"], messages, context)
        return redirect(url_for("home", convocatoria=code, _anchor="asistente"))
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True)