"""Reglas del perfil declarado, independientes de la base de datos."""
import re
import unicodedata


def experience_error(years):
    if not re.fullmatch(r"[0-9]+", years):
        return "Escriba los años completos de experiencia, de 0 a 200."
    # Compare sin convertir una cadena arbitrariamente grande a entero.
    significant = years.lstrip("0")
    if len(significant) > 3 or (len(significant) == 3 and significant > "200"):
        return "Fuera de rango: escriba un número entre 0 y 200."
    if len(years) > 3:
        return "Escriba hasta tres dígitos."
    return ""


def rubros_error(value):
    if not value:
        return "Escriba al menos un nombre o código de rubro."
    if len(value) > 500:
        return "Use como máximo 500 caracteres."
    if "." in value:
        return "No use puntos. Separe los rubros con comas."
    for part in value.split(","):
        item = part.strip()
        if re.fullmatch(r"[0-9]{4}", item):
            continue
        if not any(c.isalpha() for c in item) or not all(
            c.isalpha() or unicodedata.category(c).startswith("M")
            or c in "0123456789 -/&+()'’" for c in item
        ):
            return "Escriba nombres o códigos de cuatro dígitos, separados con comas."
    return ""
