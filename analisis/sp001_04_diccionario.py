import pandas as pd

from utils import ANIOS, COLUMNAS_USADAS, cargar, guardar

DESCRIPCION = {
    "codigoconvocatoria": ("Identificador de la convocatoria", "Llave de cruce"),
    "codigo_convocatoria": ("Identificador de la convocatoria", "Llave de cruce"),
    "n_item": ("Número del ítem dentro de la convocatoria", "Llave de cruce"),
    "objetocontractual": ("Tipo de objeto: Bien, Servicio, Obra o Consultoría de Obra",
                          "Filtro: solo bienes"),
    "tipoprocesoseleccion": ("Tipo de procedimiento de selección",
                             "Filtro: excluir contrataciones directas"),
    "moneda": ("Moneda de la contratación", "Filtro: moneda nacional"),
    "cantidad_adjudicado_item": ("Cantidad adjudicada del ítem", "Cálculo del precio unitario"),
    "monto_adjudicado_item_soles": ("Monto adjudicado del ítem, en soles",
                                    "Cálculo del precio unitario"),
    "monto_referencial_item_soles": ("Valor referencial del ítem, en soles",
                                     "Relación precio adjudicado / referencial"),
    "unidad_medida": ("Unidad de medida del ítem", "Agrupación por código y unidad"),
    "entidad_departamento": ("Departamento de la entidad compradora",
                             "Adjudicaciones de La Libertad"),
    "codigoitem": ("Código CUBSO del ítem", "Agrupación y catálogo del clasificador"),
    "itemcubso": ("Nombre del bien según el catálogo CUBSO", "Catálogo del clasificador"),
    "ruc_codigo_postor": ("RUC o código del postor", "Conteo de postores por ítem"),
}

filas = []
for fuente, columnas in COLUMNAS_USADAS.items():
    datos = {anio: cargar(fuente, anio) for anio in ANIOS}
    for col in columnas:
        descripcion, uso = DESCRIPCION[col]
        fila = {"fuente": fuente, "columna": col, "tipo": str(datos[2025][col].dtype)}
        for anio in ANIOS:
            fila[f"pct_lleno_{anio}"] = round(100 * datos[anio][col].notna().mean(), 1)
        fila["descripcion"] = descripcion
        fila["uso_en_el_proyecto"] = uso
        filas.append(fila)

diccionario = pd.DataFrame(filas)
print(diccionario.drop(columns=["descripcion", "uso_en_el_proyecto"]).to_string(index=False))
guardar(diccionario, "sp001/04_diccionario_datos.csv")
