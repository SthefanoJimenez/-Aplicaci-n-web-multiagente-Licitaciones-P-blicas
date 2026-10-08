import config
from agentes import clasificador


class DescripcionInvalida(ValueError):
    """La descripción está vacía o es demasiado larga."""


SIN_EVIDENCIA_PARA_EVALUAR = "sin_evidencia_para_evaluar"
CANDIDATOS = "candidatos"


def identificar_bien(descripcion):

    descripcion = (descripcion or "").strip()
    if not descripcion:
        raise DescripcionInvalida("Escriba una descripción del bien para buscarlo en el catálogo.")
    if len(descripcion) > config.LARGO_MAXIMO_DESCRIPCION:
        raise DescripcionInvalida(
            f"La descripción puede tener hasta {config.LARGO_MAXIMO_DESCRIPCION} caracteres. "
            "Describa solo el bien: qué es, su material y su medida o presentación."
        )

    resultado = clasificador.clasificar(descripcion)
    if not resultado["candidatos"]:
        return {"resultado": SIN_EVIDENCIA_PARA_EVALUAR, "candidatos": []}
    return {"resultado": CANDIDATOS, "candidatos": resultado["candidatos"]}