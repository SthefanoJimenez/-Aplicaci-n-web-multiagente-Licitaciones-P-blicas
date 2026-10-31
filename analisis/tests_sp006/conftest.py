"""Hace que las pruebas encuentren los scripts de analisis/ sin importar desde qué
carpeta se ejecute pytest (raíz del proyecto, analisis/ o el botón del IDE).

Los scripts se importan por su nombre (import sp005_01_tender_source), no como
"from analisis import ...", porque analisis/ no es un paquete de Python.
"""
import sys
from pathlib import Path

ANALISIS_DIR = Path(__file__).resolve().parent.parent
if str(ANALISIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALISIS_DIR))