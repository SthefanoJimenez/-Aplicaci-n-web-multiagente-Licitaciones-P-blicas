"""HU-001: ejecuta las 30 consultas de prueba con el agente clasificador real.

Registra los candidatos devueltos y verifica el criterio de aceptación:
el 100% de los candidatos mostrados existe en el catálogo.

Consume la API de Gemini (2 llamadas por consulta, unos 1,500 tokens cada una).
Ejecutar desde la carpeta app/:  python tests/ejecutar_consultas_hu001.py
"""
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import datos  # noqa: E402
import llm  # noqa: E402
from agentes import clasificador  # noqa: E402

PAUSA_SEGUNDOS = 8
CARPETA = Path(__file__).parent
consultas = pd.read_csv(CARPETA / "consultas_hu001.csv")

filas = []
for c in consultas.itertuples():
    fila = {"id": c.id, "tipo": c.tipo, "descripcion": c.descripcion, "esperado": c.esperado}
    try:
        r = clasificador.clasificar(c.descripcion)
        codigos = [b["codigo"] for b in r["candidatos"]]
        referencia = None if pd.isna(c.codigo_referencia) else int(c.codigo_referencia)
        fila.update({
            "resultado": "candidatos" if codigos else "sin_evidencia",
            "candidatos": " ".join(map(str, codigos)),
            "nombres": " | ".join(b["nombre"] for b in r["candidatos"]),
            "descripcion_reformulada": r["descripcion_reformulada"],
            "codigos_descartados": " ".join(map(str, r["descartados"])),
            "todos_en_catalogo": all(cod in datos.CODIGOS_VALIDOS for cod in codigos),
            "codigo_referencia": referencia,
            "referencia_entre_candidatos": (referencia in codigos) if referencia else None,
            "error": "",
        })
        marca = "✔" if fila["resultado"] == c.esperado else "≠"
        print(f"{c.id:>2} {marca} {fila['resultado']:<13} {fila['candidatos']:<22} {c.descripcion}")
    except llm.ErrorModelo as e:
        fila.update({"resultado": "error", "error": str(e)})
        print(f"{c.id:>2} ERROR del modelo: {e}")
    filas.append(fila)
    time.sleep(PAUSA_SEGUNDOS)

detalle = pd.DataFrame(filas)
detalle.to_csv(CARPETA / "resultados_hu001.csv", index=False, encoding="utf-8-sig")

respondidas = detalle[detalle["error"] == ""]
con_candidatos = respondidas[respondidas["resultado"] == "candidatos"]
con_referencia = respondidas[respondidas["codigo_referencia"].notna()]
print("\n### RESUMEN")
print(f"Consultas respondidas: {len(respondidas)} de {len(detalle)}")
print(f"Candidatos que existen en el catálogo: "
      f"{100 * con_candidatos['todos_en_catalogo'].mean() if len(con_candidatos) else 100:.0f}%  "
      f"(criterio de aceptación: 100%)")
print(f"Códigos inexistentes propuestos por el modelo y descartados por la validación: "
      f"{(respondidas['codigos_descartados'].fillna('') != '').sum()} consultas")
print(f"Resultado igual al esperado: {(respondidas['resultado'] == respondidas['esperado']).sum()} de {len(respondidas)}")
print(f"Bien de referencia entre los candidatos: "
      f"{int(con_referencia['referencia_entre_candidatos'].fillna(False).sum())} de {len(con_referencia)}")
print(f"\nDetalle guardado en {CARPETA / 'resultados_hu001.csv'}")