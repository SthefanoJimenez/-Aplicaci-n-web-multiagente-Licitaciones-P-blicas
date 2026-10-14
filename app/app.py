from flask import Flask, redirect, render_template, request, session, url_for

import config
import orchestrator
import rules
from services import data, llm

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


@app.context_processor
def common_context():
    return {"app_name": config.APP_NAME, "max_length": config.MAX_DESCRIPTION_LENGTH}


@app.template_filter("pen")
def format_pen(value):
    """Soles: S/ 1,234.56. Los precios menores a S/ 0.10 se muestran con 4 decimales."""
    decimals = 2 if value >= 0.1 else 4
    return f"S/ {value:,.{decimals}f}"


@app.template_filter("number")
def format_number(value):
    """Entero si no tiene decimales (5); si los tiene, con uno (2.5)."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


@app.template_filter("percent")
def format_percent(value):
    """Porcentaje con un decimal: 0.4%, 23.2%."""
    return f"{value:.1f}%"


UNIT_TEXT = {"M3": "m³", "M2": "m²", "Pie 2": "pie²", "Galon": "galón"}


@app.template_filter("unit_text")
def unit_text(unit):
    """Unidad de medida del catálogo escrita para una frase: "por galón", "por m³"."""
    return UNIT_TEXT.get(unit, str(unit).lower())


@app.get("/")
def describe():
    """Pantalla 1: descripción del bien."""
    retry = bool(request.args.get("reintentar"))
    return render_template(
        "describe.html",
        description=session.get("description", "") if retry else "",
        retry=retry,
    )


@app.post("/candidatos")
def candidates():
    """Pantalla 2: selección del código, o resultado sin evidencia."""
    description = request.form.get("description", "")
    try:
        response = orchestrator.identify_good(description)
    except orchestrator.InvalidDescription as e:
        return render_template("describe.html", description=description, error=str(e)), 400
    except llm.ModelError:
        return render_template("error.html", description=description.strip()), 503

    session["description"] = description.strip()
    session.pop("code", None)
    if response["result"] == orchestrator.NO_EVIDENCE:
        session.pop("candidates", None)
        return render_template(
            "result.html", description=description.strip(), min_catalog=rules.MIN_CATALOG
        )

    session["candidates"] = [c["code"] for c in response["candidates"]]
    return render_template("candidates.html", description=description.strip(), candidates=response["candidates"])


@app.get("/candidatos")
def back_to_candidates():
    """Vuelve a la pantalla 2 con los candidatos ya encontrados, sin llamar otra vez al modelo."""
    codes = [c for c in session.get("candidates", []) if c in data.GOODS]
    if not codes:
        return redirect(url_for("describe"))
    return render_template(
        "candidates.html",
        description=session.get("description", ""),
        candidates=[data.GOODS[c] for c in codes],
        selected=session.get("code") if session.get("code") in codes else None,
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
    """Pantalla 3: rango de precios (HU-002) y competencia (HU-003) del bien confirmado."""
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
        has_evidence=response["result"] == orchestrator.RANGE_WITH_EVIDENCE,
        min_verdict=rules.MIN_VERDICT,
    )


if __name__ == "__main__":
    app.run(debug=True)