"""Habilitaciones que piden las bases (licencias, autorizaciones, registros sectoriales). 24/11/2026.

Antes, un documento de la empresa se reconocía como habilitación solo si el PDF se llamaba igual. Ahora la
empresa dice qué habilitación es al subirlo (Mis documentos) y el semáforo compara por grupo: las bases escriben
la misma habilitación de muchas formas («autorización e inscripción vigente en el Registro de Hidrocarburos…»,
«registro en Osinergmin»).

El catálogo sale de las bases leídas al 24/11/2026: 173 de 350 bases de bienes analizadas piden al menos una
habilitación; las más pedidas son la autorización sanitaria de funcionamiento y el Registro de Hidrocarburos.
Las variantes se comparan sin tildes ni mayúsculas y en este orden (la primera que coincide gana dentro de su
grupo; un texto puede pedir varias).
"""
import re
import unicodedata

# clave: (nombre que ve la empresa, variantes como las escriben las bases)
LICENSES = {
    "hidrocarburos": ("Registro de Hidrocarburos de Osinergmin",
                      ("registro de hidrocarburos", "osinergmin")),
    "bienes_fiscalizados": ("Registro para el Control de Bienes Fiscalizados",
                            ("bienes fiscalizados",)),
    "autorizacion_sanitaria": ("Autorización sanitaria de funcionamiento",
                               ("autorizacion sanitaria",)),
    "registro_sanitario": ("Registro sanitario del producto",
                           ("registro sanitario", "notificacion sanitaria")),
    "digemid": ("Autorización de funcionamiento de DIGEMID",
                ("digemid",)),
    "bpa": ("Certificado de Buenas Prácticas de Almacenamiento (BPA)",
            ("buenas practicas de almacenamiento", r"\bbpa\b")),
    "bpm": ("Certificado de Buenas Prácticas de Manufactura (BPM)",
            ("buenas practicas de manufactura", r"\bbpm\b")),
    "haccp": ("Validación técnica oficial del plan HACCP",
              ("haccp",)),
    "certificado_conformidad": ("Certificado de conformidad",
                                ("certificado de conformidad",)),
    "saneamiento_ambiental": ("Certificado de saneamiento ambiental",
                              ("saneamiento ambiental",)),
    "licencia_funcionamiento": ("Licencia de funcionamiento",
                                ("licencia de funcionamiento", "licencia municipal")),
    "itse": ("Certificado de Inspección Técnica de Seguridad en Edificaciones (ITSE)",
             (r"\bitse\b", "inspeccion tecnica de seguridad", "defensa civil")),
    "poliza_rc": ("Póliza de seguro de responsabilidad civil",
                  ("responsabilidad civil",)),
    "sucamec": ("Autorización de SUCAMEC",
                ("sucamec",)),
}
OTHER = "otra"            # «Otra habilitación»: la empresa escribe su nombre
CHOICES = {key: label for key, (label, _) in LICENSES.items()} | {OTHER: "Otra (escriba su nombre)"}

# Gemini a veces clasifica como habilitación lo que es del personal clave (4 bases al 24/11/2026).
PERSONAL = re.compile(r"titulo profesional|grado academico|colegiatura|habilidad profesional|"
                      r"colegio de ingenieros|colegio profesional")
_STOP = {"de", "del", "la", "el", "los", "las", "y", "en", "o", "para", "por", "con", "a", "al", "copia", "simple",
         "vigente", "vigencia", "registro", "constancia", "certificado", "autorizacion", "licencia", "resolucion",
         "inscripcion", "documento", "emitido", "emitida", "sus", "su"}


def plain(text):
    text = unicodedata.normalize("NFD", text or "")
    return " ".join("".join(c for c in text if unicodedata.category(c) != "Mn").lower().split())


def _found(alias, text):
    return re.search(alias, text) if alias.startswith(r"\b") else alias in text


def kinds_in(text):
    """Claves del catálogo que nombra un texto de las bases, en el orden del catálogo. [] si ninguna."""
    normalized = plain(text)
    return [key for key, (_, aliases) in LICENSES.items() if any(_found(a, normalized) for a in aliases)]


def is_personal(text):
    return bool(PERSONAL.search(plain(text)))


def label(key, name=None):
    if key == OTHER:
        return (name or "").strip() or "Otra habilitación"
    return LICENSES.get(key, (name or key or "", ()))[0]


def words(text):
    """Palabras significativas de un nombre libre, para comparar «otra» habilitación con lo que piden las bases."""
    return {w for w in re.findall(r"[a-z0-9]{4,}", plain(text)) if w not in _STOP}


def validate(form, document_type):
    """(license_kind, license_name, errores) del formulario de Mis documentos."""
    if document_type != "habilitacion":
        return None, None, {}
    kind = (form.get("license_kind") or "").strip()
    name = " ".join((form.get("license_name") or "").split())[:160]
    if kind not in CHOICES:
        return None, None, {"license_kind": "Elija qué habilitación es."}
    if kind == OTHER and len(name) < 3:
        return kind, name, {"license_name": "Escriba el nombre de la habilitación, como figura en el documento."}
    return kind, (name if kind == OTHER else None), {}