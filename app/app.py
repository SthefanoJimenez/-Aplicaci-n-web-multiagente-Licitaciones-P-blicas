"""Rutas de la aplicación: una por paso del flujo (SP-003)."""
from flask import Flask, redirect, render_template, request, session, url_for

import config
import datos
import llm
import orquestador
import reglas

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


@app.context_processor
def datos_comunes():
    return {"app_nombre": config.APP_NOMBRE, "largo_maximo": config.LARGO_MAXIMO_DESCRIPCION}


@app.template_filter("soles")
def formato_soles(valor):
    """S/ 1,234.56. Los precios menores a S/ 0.10 se muestran con 4 decimales."""
    decimales = 2 if valor >= 0.1 else 4
    return f"S/ {valor:,.{decimales}f}"


UNIDADES_EN_TEXTO = {"M3": "m³", "M2": "m²", "Pie 2": "pie²", "Galon": "galón"}


@app.template_filter("unidad")
def unidad_en_texto(unidad):
    """Unidad de medida del catálogo escrita para una frase: "por galón", "por m³"."""
    return UNIDADES_EN_TEXTO.get(unidad, str(unidad).lower())


@app.get("/")
def describir():
    """Pantalla 1: descripción del bien."""
    return render_template(
        "describir.html",
        descripcion=session.get("descripcion", "") if request.args.get("reintentar") else "",
        reintentar=bool(request.args.get("reintentar")),
    )


@app.post("/candidatos")
def candidatos():
    """Pantalla 2: selección del código, o resultado sin evidencia."""
    descripcion = request.form.get("descripcion", "")
    try:
        respuesta = orquestador.identificar_bien(descripcion)
    except orquestador.DescripcionInvalida as e:
        return render_template("describir.html", descripcion=descripcion, error=str(e)), 400
    except llm.ErrorModelo:
        return render_template("error.html", descripcion=descripcion.strip()), 503

    session["descripcion"] = descripcion.strip()
    session.pop("codigo", None)
    if respuesta["resultado"] == orquestador.SIN_EVIDENCIA_PARA_EVALUAR:
        session.pop("candidatos", None)
        return render_template(
            "resultado.html", descripcion=descripcion.strip(), minimo_catalogo=reglas.MIN_CATALOGO
        )

    session["candidatos"] = [c["codigo"] for c in respuesta["candidatos"]]
    return render_template("candidatos.html", descripcion=descripcion.strip(), candidatos=respuesta["candidatos"])


@app.get("/candidatos")
def volver_a_candidatos():
    """Vuelve a la pantalla 2 con los candidatos ya encontrados, sin llamar otra vez al modelo."""
    codigos = [c for c in session.get("candidatos", []) if c in datos.BIENES]
    if not codigos:
        return redirect(url_for("describir"))
    return render_template(
        "candidatos.html",
        descripcion=session.get("descripcion", ""),
        candidatos=[datos.BIENES[c] for c in codigos],
        seleccionado=session.get("codigo") if session.get("codigo") in codigos else None,
    )


@app.post("/confirmar")
def confirmar():
    """Guarda el código confirmado. Solo acepta uno de los candidatos ofrecidos."""
    codigo = request.form.get("codigo", type=int)
    if codigo not in session.get("candidatos", []) or codigo not in datos.CODIGOS_VALIDOS:
        return redirect(url_for("describir"))
    session["codigo"] = codigo
    return redirect(url_for("mercado"))


@app.get("/mercado")
def mercado():
    """Pantalla 3: rango de precios del bien confirmado (HU-002)."""
    codigo = session.get("codigo")
    if codigo not in datos.BIENES:
        return redirect(url_for("describir"))
    respuesta = orquestador.analizar_mercado(codigo)
    if respuesta["resultado"] == orquestador.SIN_EVIDENCIA_PARA_EVALUAR:
        return render_template(
            "resultado.html", descripcion=datos.BIENES[codigo]["nombre"], minimo_catalogo=reglas.MIN_CATALOGO
        )
    return render_template(
        "mercado.html",
        m=respuesta["mercado"],
        con_evidencia=respuesta["resultado"] == orquestador.RANGO_CON_EVIDENCIA,
        minimo_veredicto=reglas.MIN_VEREDICTO,
    )


if __name__ == "__main__":
    app.run(debug=True)