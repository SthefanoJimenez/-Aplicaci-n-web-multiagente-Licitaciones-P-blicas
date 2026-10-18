"""SP-004 · Paso previo: listar los modelos de Gemini disponibles con su clave.

Muestra los modelos que permiten generar contenido, para elegir los dos que
se compararán. Revise en Google AI Studio cuáles están en la capa gratuita.

Requiere la variable GEMINI_API_KEY en un archivo .env (ver .env.example).
Uso:  python sp004_01_listar_modelos.py
"""
import os

from dotenv import load_dotenv
from google import genai

load_dotenv()
cliente = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

print("Modelos disponibles para generar contenido:\n")
for modelo in cliente.models.list():
    acciones = getattr(modelo, "supported_actions", None) or []
    if "generateContent" in acciones:
        print(f"  {modelo.name.removeprefix('models/'):45s} {modelo.display_name or ''}")

