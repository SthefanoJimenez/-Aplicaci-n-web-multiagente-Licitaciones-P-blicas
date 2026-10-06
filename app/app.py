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
    if respuesta["resultado"] == orquestador.SIN_EVIDENCIA_PARA_EVALUAR:
        session.pop("candidatos", None)
        return render_template(
            "resultado.html", descripcion=descripcion.strip(), minimo_catalogo=reglas.MIN_CATALOGO
        )

    session["candidatos"] = [c["codigo"] for c in respuesta["candidatos"]]
    return render_template("candidatos.html", descripcion=descripcion.strip(), candidatos=respuesta["candidatos"])


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
    """Pantalla 3. Provisional hasta HU-002: muestra el bien confirmado."""
    codigo = session.get("codigo")
    if codigo not in datos.BIENES:
        return redirect(url_for("describir"))
    return render_template("mercado.html", bien=datos.BIENES[codigo])


if __name__ == "__main__":
    app.run(debug=True)