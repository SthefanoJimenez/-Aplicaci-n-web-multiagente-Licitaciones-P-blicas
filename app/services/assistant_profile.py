"""Declaraciones conversacionales y guía de preparación; no calcula semáforos."""
from __future__ import annotations
from typing import Dict, Any
import re
import unicodedata

_NEGATIVE_SALES = re.compile(
    r"\b(?:nunca|jam[aá]s|no)\b[^.;\n]{0,40}?\b(?:vendid[oa]s?|vendo|vende|ventas?)\b"
)

_POSITIVE_SALES = re.compile(
    r"\b(?:he|hemos|ya|vengo)\s+vendid[oa]s?\b|\bvendo al estado\b|\bproveedor(?: habitual)?\b"
)

def extract_profile_updates(text: str, current_profile: Dict[str, Any]) -> Dict[str, Any]:
    """Actualiza campos del perfil a partir del mensaje del usuario de manera determinista y robusta."""
    p = dict(current_profile or {})
    text_lower = text.lower()

    # RUC
    ruc_match = re.search(r"\b(10\d{9}|20\d{9})\b", text)
    if ruc_match:
        p["ruc"] = ruc_match.group(1)

    # RNP
    if "no tengo rnp" in text_lower or "sin rnp" in text_lower or "no cuento con rnp" in text_lower:
        p["has_rnp"] = False
    elif "tengo rnp" in text_lower or "con rnp" in text_lower or "rnp vigente" in text_lower or "inscrito en el rnp" in text_lower:
        p["has_rnp"] = True

    # Ventas al estado / experiencia previa
    if (_NEGATIVE_SALES.search(text_lower) or "sin ventas al estado" in text_lower
            or "primera vez" in text_lower):
        p["sold_to_state"] = False
    elif _POSITIVE_SALES.search(text_lower):
        p["sold_to_state"] = True

    # Experiencia acumulada (S/ o soles)
    exp_match = re.search(r"(?:facturaci[oó]n|experiencia|ventas)[\s\w]*(?:de|por)?\s*s\/?\.?\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:\.[0-9]+)?|[0-9]+)\s*(mil|millones)?", text_lower)
    if exp_match:
        val_str = exp_match.group(1).replace(",", "")
        try:
            val = float(val_str)
            unit = exp_match.group(2)
            if unit == "mil":
                val *= 1000
            elif unit == "millones":
                val *= 1000000
            p["experience_amount"] = val
        except ValueError:
            pass
    elif any(phrase in text_lower for phrase in ("sin experiencia", "no tengo experiencia", "cero experiencia", "0 experiencia", "0 soles")):
        p["experience_amount"] = 0.0

    # Rubro o actividad
    if any(w in text_lower for w in ("combustible", "diesel", "diésel", "gasolina", "gasohol", "hidrocarburos", "grifo", "estacion")):
        p["rubro"] = "1510"
        p["rubro_name"] = "Combustibles y lubricantes"
    elif any(w in text_lower for w in ("vehiculo", "vehículo", "camioneta", "camion", "camión", "motocicleta", "auto", "transporte")):
        p["rubro"] = "2510"
        p["rubro_name"] = "Vehículos de motor"
    elif any(w in text_lower for w in ("cemento", "arena", "ferreteria", "ferretería", "construccion", "construcción", "ladrillo")):
        p["rubro"] = "3011"
        p["rubro_name"] = "Materiales de construcción"
    elif any(w in text_lower for w in ("computadora", "laptop", "software", "tecnologia", "tecnología", "impresora")):
        p["rubro"] = "4321"
        p["rubro_name"] = "Equipos informáticos"

    # Habilitaciones específicas
    if "sin osinergmin" in text_lower or "no tengo osinergmin" in text_lower:
        p["has_osinergmin"] = False
    elif "osinergmin" in text_lower or "registro de hidrocarburos" in text_lower:
        p["has_osinergmin"] = True

    # Consorcio
    if "sin consorcio" in text_lower or "no tengo consorcio" in text_lower or "sin socio" in text_lower or "solo" in text_lower or "por mi cuenta" in text_lower:
        p["has_consortium"] = False
        p["consortium_experience"] = False
        p["consortium_habilitation"] = False
    elif any(w in text_lower for w in ("consorcio", "asociarme", "asociarme con otra empresa", "con un socio", "en consorcio")):
        p["has_consortium"] = True
        # socio con experiencia o habilitación
        if "socio con experiencia" in text_lower or "socio aporta experiencia" in text_lower or "socio tiene experiencia" in text_lower or "socio que factura" in text_lower:
            p["consortium_experience"] = True
        if "socio con habilitacion" in text_lower or "socio con osinergmin" in text_lower:
            p["consortium_habilitation"] = True

    return p

def check_profile_node(state: AssistantState) -> Dict[str, Any]:
    """Revisa si se tiene al menos el rubro o tipo de bien que ofrece la empresa."""
    profile = state.get("profile", {})
    query = state.get("query", "").lower()

    # Una pregunta de preparación general no requiere conocer el rubro primero.
    normalized = "".join(
        char for char in unicodedata.normalize("NFD", query)
        if unicodedata.category(char) != "Mn"
    )
    asks_for_material = re.search(r"\bque\s+(?:informacion|datos|documentos)\b", normalized)
    asks_to_prepare = re.search(r"\b(?:tener|preparar|necesito|necesita|necesitan|necesitamos)\b", normalized)
    has_specific_code = re.search(r"\b\d{4,8}\b", query)
    if asks_for_material and asks_to_prepare and not has_specific_code:
        return {"missing_data": None, "status": "general_guidance"}

    # Si la consulta pregunta a qué postular pero no conocemos el rubro ni la actividad
    if not profile.get("rubro") and not any(w in query for w in ("1510", "2510", "3011", "combustible", "vehiculo", "vehículo", "cemento")):
        # Verificar si menciona un código específico de convocatoria (ej. "1001" o "1256397")
        code_match = re.search(r"\b(\d{4,8})\b", query)
        if not code_match:
            missing_msg = (
                "Para indicarte con precisión a qué convocatorias puedes presentarte sin riesgo de rechazo, "
                "necesito conocer **qué bienes o productos vende tu empresa** (por ejemplo: combustibles/diésel, "
                "vehículos, materiales de construcción, etc.). ¿Cuál es el rubro o producto principal de tu negocio?"
            )
            return {"missing_data": missing_msg, "status": "need_data"}

    return {"missing_data": None, "status": "profile_ready"}

def preparation_guide_node(state: AssistantState) -> Dict[str, Any]:
    """Responde preguntas generales de preparación sin consultar ni evaluar procesos."""
    return {"response": (
        "### Qué tener listo para revisar una convocatoria\n\n"
        "- **Convocatoria:** su código o enlace, las bases y sus anexos, y el plazo de presentación.\n"
        "- **Empresa y productos:** giro, rubros y descripción de lo que ofrece tu empresa.\n"
        "- **Experiencia:** contratos similares, montos facturados y documentos que los sustenten.\n"
        "- **Registros y habilitaciones:** información del RNP y de las autorizaciones que correspondan según las bases.\n"
        "- **Documentos:** constancias, certificados y sus fechas de vigencia para contrastarlos con lo solicitado.\n"
        "- **Consorcio, si corresponde:** integrantes, aportes y documentación de cada participante.\n\n"
        "Esta es una lista de preparación; **todavía no es un veredicto de cumplimiento**. "
        "Cada requisito debe comprobarse en las bases de la convocatoria concreta.\n\n"
        "Para continuar, comparte el **código de la convocatoria** o indica **qué producto vende tu empresa**."
    )}
