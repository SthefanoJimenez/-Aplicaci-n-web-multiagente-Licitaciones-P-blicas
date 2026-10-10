
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
LA_LIBERTAD = "LA LIBERTAD"


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


def iguales(a, b):
    """Iguales dentro de la tolerancia; dos valores ausentes también son iguales."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= TOLERANCIA


catalogo = leer_csv("catalogo.csv")
adjudicaciones = leer_csv("adjudicaciones.csv")

# Selección de los 30 grupos, estratificada y reproducible (la misma de HU-002)
azar = random.Random(SEMILLA)
seleccion = []
for nombre_estrato, desde, hasta in ESTRATOS:
    del_estrato = [b for b in catalogo if desde <= int(b["n_adjudicaciones"]) <= hasta]
    for bien in sorted(azar.sample(del_estrato, GRUPOS_POR_ESTRATO), key=lambda b: int(b["n_adjudicaciones"])):
        seleccion.append((nombre_estrato, bien))

filas, datos_por_grupo = [], []
for numero, (estrato, bien) in enumerate(seleccion, start=1):
    codigo, unidad = int(bien["codigoitem"]), bien["unidad_medida"]

    # Control: filtrado por código y unidad, y cálculo con la biblioteca estándar
    propias = [a for a in adjudicaciones if int(a["codigoitem"]) == codigo and a["unidad_medida"] == unidad]
    precios = sorted(float(a["precio_unitario"]) for a in propias)
    anios = [int(a["anio"]) for a in propias]
    postores = sorted(int(a["n_postores"]) for a in propias if a["n_postores"] != "")
    departamentos = [a["entidad_departamento"] for a in propias]
    n_unico = sum(1 for p in postores if p == 1)

    control = {
        "n": len(precios),
        "periodo": f"{min(anios)}-{max(anios)}",
        "p25": percentil_lineal(precios, 0.25),
        "mediana": percentil_lineal(precios, 0.50),
        "p75": percentil_lineal(precios, 0.75),
        "evidencia": evidencia_control(len(precios)),
        "n_con_postores": len(postores),
        "mediana_postores": percentil_lineal(postores, 0.50) if postores else None,
        "n_postor_unico": n_unico,
        "pct_postor_unico": 100 * n_unico / len(postores) if postores else None,
        "n_la_libertad": sum(1 for d in departamentos if d == LA_LIBERTAD),
    }
    q1, q2, q3 = statistics.quantiles(precios, n=4, method="inclusive")
    mediana_postores_estandar = statistics.median(postores) if postores else None
    datos_por_grupo.append((codigo, precios, postores, departamentos))

    # Aplicación: agente analista
    app = analista.analizar(codigo)

    fila = {
        "grupo": numero, "estrato": estrato, "codigo": codigo, "nombre": bien["itemcubso"].strip(),
        "unidad": unidad,
        "n_app": app["n_adjudicaciones"], "n_control": control["n"],
        "periodo_app": f"{app['anio_desde']}-{app['anio_hasta']}", "periodo_control": control["periodo"],
        "evidencia_app": app["evidencia"], "evidencia_control": control["evidencia"],
    }

    # HU-002: rango de precios
    diferencias = []
    for medida, estandar in (("p25", q1), ("mediana", q2), ("p75", q3)):
        dif = abs(app[medida] - control[medida])
        diferencias += [dif, abs(app[medida] - estandar)]
        fila.update({
            f"{medida}_app": app[medida],
            f"{medida}_control": control[medida],
            f"{medida}_statistics": estandar,
            f"{medida}_diferencia": dif,
        })
    fila["diferencia_maxima_precios"] = max(diferencias)
    coincide_precios = (
        fila["n_app"] == fila["n_control"]
        and fila["periodo_app"] == fila["periodo_control"]
        and fila["evidencia_app"] == fila["evidencia_control"]
        and fila["diferencia_maxima_precios"] <= TOLERANCIA
    )

    # HU-003: competencia
    for medida in ("n_con_postores", "mediana_postores", "n_postor_unico", "pct_postor_unico", "n_la_libertad"):
        fila[f"{medida}_app"] = app[medida]
        fila[f"{medida}_control"] = control[medida]
    fila["mediana_postores_statistics"] = mediana_postores_estandar
    coincide_competencia = (
        app["n_con_postores"] == control["n_con_postores"]
        and app["n_postor_unico"] == control["n_postor_unico"]
        and app["n_la_libertad"] == control["n_la_libertad"]
        and iguales(app["mediana_postores"], control["mediana_postores"])
        and iguales(app["mediana_postores"], mediana_postores_estandar)
        and iguales(app["pct_postor_unico"], control["pct_postor_unico"])
    )

    fila["coincide_precios"] = coincide_precios
    fila["coincide_competencia"] = coincide_competencia
    fila["coincide"] = coincide_precios and coincide_competencia
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
hoja_postores = libro.create_sheet("Postores")
hoja_departamentos = libro.create_sheet("Departamentos")
negrita, azul = Font(name="Arial", bold=True), Font(name="Arial", color="0000FF")

for columna, (codigo, precios, postores, departamentos) in enumerate(datos_por_grupo, start=1):
    for hoja, valores in ((hoja_precios, precios), (hoja_postores, postores), (hoja_departamentos, departamentos)):
        hoja.cell(row=1, column=columna, value=codigo).font = negrita
        for fila_excel, valor in enumerate(valores, start=2):
            hoja.cell(row=fila_excel, column=columna, value=valor)

encabezados = [
    "Grupo", "Código", "Unidad",
    # HU-002 (columnas D a L)
    "N (Excel)", "P25 (Excel)", "Mediana (Excel)", "P75 (Excel)",
    "N (app)", "P25 (app)", "Mediana (app)", "P75 (app)", "Diferencia precios",
    # HU-003 (columnas M a U)
    "Con postores (Excel)", "Mediana postores (Excel)", "% postor único (Excel)", "La Libertad (Excel)",
    "Con postores (app)", "Mediana postores (app)", "% postor único (app)", "La Libertad (app)",
    "Diferencia competencia",
    "Coincide",
]
for columna, texto in enumerate(encabezados, start=1):
    hoja_control.cell(row=1, column=columna, value=texto).font = negrita

ultima_fila_datos = max(len(p) for _, p, _, _ in datos_por_grupo) + 1
for i, fila in enumerate(filas, start=2):
    letra = get_column_letter(i - 1)
    precios = f"Precios!${letra}$2:${letra}${ultima_fila_datos}"
    postores = f"Postores!${letra}$2:${letra}${ultima_fila_datos}"
    departamentos = f"Departamentos!${letra}$2:${letra}${ultima_fila_datos}"
    valores = [
        fila["grupo"], fila["codigo"], fila["unidad"],
        f"=COUNT({precios})",
        f"=_xlfn.PERCENTILE.INC({precios},0.25)",
        f"=MEDIAN({precios})",
        f"=_xlfn.PERCENTILE.INC({precios},0.75)",
        fila["n_app"], fila["p25_app"], fila["mediana_app"], fila["p75_app"],
        f"=MAX(ABS(E{i}-I{i}),ABS(F{i}-J{i}),ABS(G{i}-K{i}))",
        f"=COUNT({postores})",
        f'=IF(M{i}=0,"",MEDIAN({postores}))',
        f'=IF(M{i}=0,"",COUNTIF({postores},1)/M{i}*100)',
        f'=COUNTIF({departamentos},"{LA_LIBERTAD}")',
        fila["n_con_postores_app"],
        "" if fila["mediana_postores_app"] is None else fila["mediana_postores_app"],
        "" if fila["pct_postor_unico_app"] is None else fila["pct_postor_unico_app"],
        fila["n_la_libertad_app"],
        f"=IF(M{i}=0,0,MAX(ABS(N{i}-R{i}),ABS(O{i}-S{i})))",
        f'=IF(AND(D{i}=H{i},L{i}<={TOLERANCIA},M{i}=Q{i},P{i}=T{i},U{i}<={TOLERANCIA}),"Sí","No")',
    ]
    for columna, valor in enumerate(valores, start=1):
        celda = hoja_control.cell(row=i, column=columna, value=valor)
        if 8 <= columna <= 11 or 17 <= columna <= 20:
            celda.font = azul   # valores copiados de la aplicación
        if columna in (5, 6, 7, 9, 10, 11):
            celda.number_format = "#,##0.000000"
        if columna in (15, 19):
            celda.number_format = "0.000000"
        if columna in (12, 21):
            celda.number_format = "0.0E+00"

ultima = len(filas) + 1
hoja_control.cell(row=ultima + 2, column=1, value="Discrepancias").font = negrita
hoja_control.cell(row=ultima + 2, column=2, value=f'=COUNTIF(V2:V{ultima},"No")').font = negrita
hoja_control.cell(row=ultima + 3, column=1,
                  value="Columnas en azul: valores calculados por el agente analista. "
                        "Columnas (Excel): fórmulas sobre las hojas Precios, Postores y Departamentos.")
anchos = [8, 10, 11, 10, 16, 16, 16, 10, 16, 16, 16, 18, 14, 16, 16, 12, 14, 16, 16, 12, 18, 10]
for columna, ancho in enumerate(anchos, start=1):
    hoja_control.column_dimensions[get_column_letter(columna)].width = ancho
for hoja in libro.worksheets:
    for fila_celdas in hoja.iter_rows():
        for celda in fila_celdas:
            if celda.font.name != "Arial":
                celda.font = Font(name="Arial", bold=celda.font.bold, color=celda.font.color)
libro.save(CARPETA / "control_hu002.xlsx")

# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------
def mostrar(valor, formato):
    return "—" if valor is None else format(valor, formato)


print("### CONTROL HU-002 y HU-003: agente analista vs. herramientas independientes\n")
print(f"{'#':>2} {'estrato':<9} {'código':>7} {'N':>5} {'P25':>14} {'mediana':>14} {'P75':>14}"
      f"  {'post.':>5} {'único':>7} {'LL':>4}  resultado")
for f in filas:
    marca = "✔" if f["coincide"] else "✘"
    print(f"{f['grupo']:>2} {f['estrato']:<9} {f['codigo']:>7} {f['n_app']:>5} "
          f"{f['p25_app']:>14,.4f} {f['mediana_app']:>14,.4f} {f['p75_app']:>14,.4f}  "
          f"{mostrar(f['mediana_postores_app'], '>5.1f')} {mostrar(f['pct_postor_unico_app'], '>6.1f')}% "
          f"{f['n_la_libertad_app']:>4}  {marca}")

print(f"\nGrupos revisados: {len(filas)}")
print(f"Rango de precios (HU-002) con discrepancias: {sum(not f['coincide_precios'] for f in filas)}")
print(f"Competencia (HU-003) con discrepancias:      {sum(not f['coincide_competencia'] for f in filas)}")
print(f"Grupos con alguna discrepancia: {sum(not f['coincide'] for f in filas)}  (criterio de aceptación: 0)")
print(f"Diferencia máxima en precios: {max(f['diferencia_maxima_precios'] for f in filas):.2e} soles")
print(f"Adjudicaciones sin dato de postores en estos grupos: "
      f"{sum(f['n_app'] - f['n_con_postores_app'] for f in filas)}")
print(f"\nDetalle: {CARPETA / 'control_hu002.csv'}")
print(f"Libro para revisar en Excel: {CARPETA / 'control_hu002.xlsx'}")