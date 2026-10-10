import csv
import math
import random
import statistics
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentes import analista  # noqa: E402

CARPETA = Path(__file__).parent
DATOS = CARPETA.parent / "datos"
SEMILLA = 2026
TOLERANCIA = 0.000001
ESTRATOS = [("5 a 9", 5, 9), ("10 a 29", 10, 29), ("30 o más", 30, math.inf)]
GRUPOS_POR_ESTRATO = 10


# ---------------------------------------------------------------------------
# Herramienta independiente: solo la biblioteca estándar de Python
# ---------------------------------------------------------------------------
def leer_csv(nombre):
    with open(DATOS / nombre, encoding="utf-8", newline="") as archivo:
        return list(csv.DictReader(archivo))


def percentil_lineal(valores_ordenados, p):
    """Interpolación lineal entre posiciones (definición de PERCENTIL.INC de Excel)."""
    posicion = (len(valores_ordenados) - 1) * p
    abajo = math.floor(posicion)
    arriba = min(abajo + 1, len(valores_ordenados) - 1)
    fraccion = posicion - abajo
    return valores_ordenados[abajo] + fraccion * (valores_ordenados[arriba] - valores_ordenados[abajo])


def evidencia_control(n):
    """Mínimos de SP-002 escritos de nuevo, sin importar reglas.py."""
    if n < 5:
        return "Sin evidencia para evaluar"
    if n < 10:
        return "Sin evidencia suficiente"
    return "Con veredicto"


catalogo = leer_csv("catalogo.csv")
adjudicaciones = leer_csv("adjudicaciones.csv")

# Selección de los 30 grupos, estratificada y reproducible
azar = random.Random(SEMILLA)
seleccion = []
for nombre_estrato, desde, hasta in ESTRATOS:
    del_estrato = [b for b in catalogo if desde <= int(b["n_adjudicaciones"]) <= hasta]
    for bien in sorted(azar.sample(del_estrato, GRUPOS_POR_ESTRATO), key=lambda b: int(b["n_adjudicaciones"])):
        seleccion.append((nombre_estrato, bien))

filas, precios_por_grupo = [], []
for numero, (estrato, bien) in enumerate(seleccion, start=1):
    codigo, unidad = int(bien["codigoitem"]), bien["unidad_medida"]

    # Control: filtrado por código y unidad, y cálculo con la biblioteca estándar
    propias = [a for a in adjudicaciones if int(a["codigoitem"]) == codigo and a["unidad_medida"] == unidad]
    precios = sorted(float(a["precio_unitario"]) for a in propias)
    anios = [int(a["anio"]) for a in propias]
    control = {
        "n": len(precios),
        "periodo": f"{min(anios)}-{max(anios)}",
        "p25": percentil_lineal(precios, 0.25),
        "mediana": percentil_lineal(precios, 0.50),
        "p75": percentil_lineal(precios, 0.75),
        "evidencia": evidencia_control(len(precios)),
    }
    q1, q2, q3 = statistics.quantiles(precios, n=4, method="inclusive")
    precios_por_grupo.append((codigo, precios))

    # Aplicación: agente analista
    app = analista.analizar(codigo)

    fila = {
        "grupo": numero, "estrato": estrato, "codigo": codigo, "nombre": bien["itemcubso"].strip(),
        "unidad": unidad,
        "n_app": app["n_adjudicaciones"], "n_control": control["n"],
        "periodo_app": f"{app['anio_desde']}-{app['anio_hasta']}", "periodo_control": control["periodo"],
        "evidencia_app": app["evidencia"], "evidencia_control": control["evidencia"],
    }
    diferencias = []
    for medida, estandar in (("p25", q1), ("mediana", q2), ("p75", q3)):
        dif = abs(app[medida] - control[medida])
        dif_estandar = abs(app[medida] - estandar)
        diferencias += [dif, dif_estandar]
        fila.update({
            f"{medida}_app": app[medida],
            f"{medida}_control": control[medida],
            f"{medida}_statistics": estandar,
            f"{medida}_diferencia": dif,
        })
    fila["diferencia_maxima"] = max(diferencias)
    fila["coincide"] = (
        fila["n_app"] == fila["n_control"]
        and fila["periodo_app"] == fila["periodo_control"]
        and fila["evidencia_app"] == fila["evidencia_control"]
        and fila["diferencia_maxima"] <= TOLERANCIA
    )
    filas.append(fila)

# ---------------------------------------------------------------------------
# Evidencia en CSV
# ---------------------------------------------------------------------------
with open(CARPETA / "control_hu002.csv", "w", encoding="utf-8-sig", newline="") as archivo:
    escritor = csv.DictWriter(archivo, fieldnames=list(filas[0]))
    escritor.writeheader()
    escritor.writerows(filas)

# ---------------------------------------------------------------------------
# Libro de Excel con fórmulas, para recalcular en una tercera herramienta
# ---------------------------------------------------------------------------
libro = Workbook()
hoja_control = libro.active
hoja_control.title = "Control"
hoja_precios = libro.create_sheet("Precios")
negrita, azul = Font(name="Arial", bold=True), Font(name="Arial", color="0000FF")

for columna, (codigo, precios) in enumerate(precios_por_grupo, start=1):
    hoja_precios.cell(row=1, column=columna, value=codigo).font = negrita
    for fila_excel, precio in enumerate(precios, start=2):
        hoja_precios.cell(row=fila_excel, column=columna, value=precio)

encabezados = ["Grupo", "Código", "Unidad", "N (Excel)", "P25 (Excel)", "Mediana (Excel)", "P75 (Excel)",
               "N (app)", "P25 (app)", "Mediana (app)", "P75 (app)", "Diferencia máxima", "Coincide"]
for columna, texto in enumerate(encabezados, start=1):
    hoja_control.cell(row=1, column=columna, value=texto).font = negrita

maximo = max(len(p) for _, p in precios_por_grupo) + 1
for i, fila in enumerate(filas, start=2):
    letra = get_column_letter(i - 1)
    rango = f"Precios!${letra}$2:${letra}${maximo}"
    valores = [
        fila["grupo"], fila["codigo"], fila["unidad"],
        f"=COUNT({rango})",
        f"=_xlfn.PERCENTILE.INC({rango},0.25)",
        f"=MEDIAN({rango})",
        f"=_xlfn.PERCENTILE.INC({rango},0.75)",
        fila["n_app"], fila["p25_app"], fila["mediana_app"], fila["p75_app"],
        f"=MAX(ABS(E{i}-I{i}),ABS(F{i}-J{i}),ABS(G{i}-K{i}))",
        f'=IF(AND(D{i}=H{i},L{i}<={TOLERANCIA}),"Sí","No")',
    ]
    for columna, valor in enumerate(valores, start=1):
        celda = hoja_control.cell(row=i, column=columna, value=valor)
        if 8 <= columna <= 11:
            celda.font = azul   # valores copiados de la aplicación
        if columna in (5, 6, 7, 9, 10, 11):
            celda.number_format = "#,##0.000000"
        if columna == 12:
            celda.number_format = "0.0E+00"

ultima = len(filas) + 1
hoja_control.cell(row=ultima + 2, column=1, value="Discrepancias").font = negrita
hoja_control.cell(row=ultima + 2, column=2, value=f'=COUNTIF(M2:M{ultima},"No")').font = negrita
hoja_control.cell(row=ultima + 3, column=1,
                  value="Columnas en azul: valores calculados por el agente analista. "
                        "Columnas N, P25, Mediana y P75 (Excel): fórmulas sobre la hoja Precios.")
for columna, ancho in zip("ABCDEFGHIJKLM", [8, 10, 11, 10, 16, 16, 16, 10, 16, 16, 16, 18, 10]):
    hoja_control.column_dimensions[columna].width = ancho
for hoja in (hoja_control, hoja_precios):
    for fila_celdas in hoja.iter_rows():
        for celda in fila_celdas:
            if celda.font.name != "Arial":
                celda.font = Font(name="Arial", bold=celda.font.bold, color=celda.font.color)
libro.save(CARPETA / "control_hu002.xlsx")

# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------
print("### CONTROL HU-002: agente analista vs. herramientas independientes\n")
print(f"{'#':>2} {'estrato':<9} {'código':>7} {'N':>5} {'periodo':<10} {'P25':>14} {'mediana':>14} {'P75':>14}  resultado")
for f in filas:
    marca = "✔" if f["coincide"] else "✘"
    print(f"{f['grupo']:>2} {f['estrato']:<9} {f['codigo']:>7} {f['n_app']:>5} {f['periodo_app']:<10} "
          f"{f['p25_app']:>14,.4f} {f['mediana_app']:>14,.4f} {f['p75_app']:>14,.4f}  {marca}")

discrepancias = sum(not f["coincide"] for f in filas)
exactos = sum(f["diferencia_maxima"] == 0 for f in filas)
print(f"\nGrupos revisados: {len(filas)}")
print(f"Grupos con discrepancias: {discrepancias}  (criterio de aceptación: 0)")
print(f"Grupos idénticos hasta el último decimal: {exactos} de {len(filas)}")
print(f"Diferencia máxima encontrada: {max(f['diferencia_maxima'] for f in filas):.2e} soles")
print(f"\nDetalle: {CARPETA / 'control_hu002.csv'}")
print(f"Libro para revisar en Excel: {CARPETA / 'control_hu002.xlsx'}")