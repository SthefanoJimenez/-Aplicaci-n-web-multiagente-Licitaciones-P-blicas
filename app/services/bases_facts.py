"""Lo que piden las bases, en datos que se pueden comparar con los de la empresa (20/11/2026).

Pedido del PO: la ficha no debe pedirle al cliente datos que vienen de las bases, ni dejar todo «por revisar».
El texto de los requisitos (la sección ya leída) y otras partes de las bases (cuantía, consorcio, personal
clave, equipamiento) se envían a Gemini, que devuelve cada requisito como un dato: tipo, monto mínimo,
profesión y meses de experiencia, registro del RNP, documento… El semáforo (services/eligibility.py) los
compara con Mi empresa, Personal clave y Mis documentos.

Solo se envían bases de licitación, que son públicas. Lo que Gemini devuelve se revisa antes de guardarlo:
un monto, una cantidad o unos meses que no están escritos en el texto de las bases se descartan (no se
inventan umbrales), y una página que no es de las bases se quita.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import unicodedata

import config

ANALYSIS_MAX_TOKENS = 8000     # la respuesta JSON de unas bases ocupa unos 1 500 tokens
VERSION = 2                      # 2 (24/11/2026): capacitación del personal clave (tema y horas por cargo)
TRAINING_VERSION = 2             # desde esta versión el análisis trae la capacitación; --actualizar-capacitaciones
SECTION_CHARS = 24000            # la sección de requisitos casi nunca pasa de 15 000 caracteres
KINDS = ("rnp", "experiencia", "experiencia_anios", "personal", "equipamiento", "habilitacion", "ventas",
         "documento", "otro")
RNP_REGISTERS = ("bienes", "servicios", "ejecutor_obras", "consultor_obras")

PROMPT = """Eres un especialista en contrataciones públicas del Perú (Ley 32069 y su reglamento). Te doy partes de
las bases de una convocatoria: la sección de REQUISITOS DE CALIFICACIÓN y, aparte, OTRAS PARTES de las bases.
Extrae SOLO lo que está escrito. No completes con lo que suele pedirse. Si un dato no está, usa null.

Devuelve un JSON con esta forma exacta:
{{
 "valor_estimado": {{"monto": número o null, "moneda": "PEN" | "USD" | null, "reservado": true | false,
                     "pagina": número o null}},
 "consorcio": {{"permitido": true | false | null, "max_integrantes": número o null,
                "participacion_minima": número (porcentaje) o null}},
 "prestaciones_accesorias": true | false | null,
 "requisitos": [
  {{"tipo": "rnp" | "experiencia" | "experiencia_anios" | "personal" | "equipamiento" | "habilitacion" |
            "ventas" | "documento" | "otro",
    "descripcion": "qué se pide, en una frase corta y sencilla",
    "monto_minimo": número o null, "monto_minimo_mype": número o null, "moneda": "PEN" | "USD" | null,
    "veces_valor_estimado": número o null,
    "anios": número o null,
    "cargo": texto o null, "profesion": texto o null, "experiencia_meses": número o null,
    "colegiado": true | false | null, "cantidad": número o null,
    "capacitacion_tema": texto o null, "capacitacion_horas": número o null,
    "momento": "oferta" | "contrato" | null,
    "registro_rnp": "bienes" | "servicios" | "ejecutor_obras" | "consultor_obras" | null,
    "documento": texto o null,
    "admite_alquiler": true | false | null,
    "pagina": número o null,
    "cita": "frase literal de las bases (máximo 300 caracteres) donde se pide"}}
 ]
}}

Reglas:
- valor_estimado: el valor estimado, valor referencial o cuantía de la contratación. Si las bases dicen que no se
  da a conocer o es reservado, "reservado": true y "monto": null.
- Un requisito por cosa que se pide. "experiencia": monto facturado acumulado en contratos similares (monto_minimo,
  o veces_valor_estimado si se pide como "N veces el valor estimado"; "anios": en los últimos N años).
  "experiencia_anios": años de experiencia de la empresa. "personal": un elemento por cargo, con profesion,
  experiencia_meses (convierte años a meses), colegiado, cantidad y momento (si se acredita con la oferta o para
  firmar el contrato). "equipamiento": máquinas, vehículos o equipos; admite_alquiler si las bases aceptan
  alquiler o compromiso de alquiler. "habilitacion": licencias, autorizaciones, registros sectoriales,
  certificaciones (con "documento" = su nombre). "rnp": inscripción en el Registro Nacional de Proveedores, con
  registro_rnp. "documento": declaraciones juradas, anexos, cartas o formatos que se llenan al presentar la
  oferta. "otro": lo demás.
- capacitacion_tema y capacitacion_horas (solo en "personal"): si las bases piden capacitación para ese cargo
  (por ejemplo, en «Capacitación del personal clave»), el tema y las horas lectivas o académicas que pide. Si
  pide varios temas, júntalos en el texto y usa el total de horas solo si lo dicen. No es la capacitación que el
  postor debe dar al personal de la entidad (eso es un factor de evaluación: no lo pongas aquí).
- monto_minimo_mype: si las bases fijan otro monto de experiencia para micro y pequeña empresa (MYPE), ese
  monto; monto_minimo es el general. Es un solo requisito, no dos.
- Los números van sin separadores de miles: 320960.00, no "320,960.00". La moneda es la que dicen las bases.
- "pagina": el número que aparece en la marca [página N] más cercana antes del texto.
- No inventes requisitos. Si la sección no trae requisitos, "requisitos": [].
{category}
=== REQUISITOS DE CALIFICACIÓN ===
{section}

=== OTRAS PARTES DE LAS BASES ===
{context}
"""


def enabled():
    """Se analiza con Gemini si hay clave y no se apagó con ANALISIS_BASES=no."""
    return bool(config.GEMINI_API_KEY) and config.BASES_ANALYSIS


def plain(text):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", text)


def section_with_pages(blocks):
    """Texto de la sección con marcas de página, a partir de los bloques de requirements.merge_chunks."""
    parts = [(f"[página {b.page}]\n" if b.page else "") + b.text for b in blocks]
    return "\n\n".join(parts)


def marked_section(section):
    """Texto de una bases_reader.Section con «[página N]» donde empieza cada página."""
    text, marks = section.text, sorted(section.page_marks or [])
    if not marks:
        return text
    parts = []
    for i, (start, page) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        if text[start:end].strip():
            parts.append(f"[página {page}]\n" + text[start:end].strip())
    return "\n\n".join(parts)


def build_prompt(section, context="", category=None):
    hint = (f"- La convocatoria es de {category}.\n" if category else "")
    return PROMPT.format(section=section[:SECTION_CHARS], context=context or "(no hay)", category=hint)


# --------------------------------------------------------------------------- revisión de lo que devuelve
def _number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    raw = str(value).strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", raw):
        raw = raw.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", raw):
        raw = raw.replace(",", ".")
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return number if number >= 0 else None


def numbers_in(text):
    """Todos los números escritos en el texto (con miles en coma o en punto), como Decimal."""
    found = set()
    text = re.sub(r"(?<=\d)[ \u00a0](?=\d{3}(?:\D|$))", "", text or "")      # «1 250 000.00»
    for raw in re.findall(r"\d[\d.,]*\d|\d", text):
        raw = raw.strip(".,")
        for candidate in {raw, raw.replace(",", ""), raw.replace(".", "").replace(",", ".")}:
            number = _number(candidate)
            if number is not None:
                found.add(number.normalize())
    words = {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
             "ocho": 8, "nueve": 9, "diez": 10, "doce": 12, "quince": 15, "veinte": 20, "veinticuatro": 24,
             "treinta": 30, "treinta y seis": 36, "cuarenta y ocho": 48, "sesenta": 60}
    normalized = plain(text)
    for word, value in words.items():
        if re.search(r"\b" + word + r"\b", normalized):
            found.add(Decimal(value))
    return found


def _written(value, written):
    return value is not None and Decimal(value).normalize() in written


def _text(value, limit):
    value = str(value).strip() if value not in (None, "") else None
    return value[:limit] if value else None


def _bool(value):
    return value if isinstance(value, bool) else None


def clean(raw, source, pages=None):
    """Revisa la respuesta del modelo contra el texto que se le envió. Devuelve el dict que se guarda."""
    raw = raw if isinstance(raw, dict) else {}
    written, pages = numbers_in(source), set(pages or [])

    def page(value):
        number = _number(value)
        number = int(number) if number is not None and number == int(number) else None
        return number if number and (not pages or number in pages) else None

    estimate = raw.get("valor_estimado") if isinstance(raw.get("valor_estimado"), dict) else {}
    amount = _number(estimate.get("monto"))
    amount = amount if amount and _written(amount, written) else None
    currency = estimate.get("moneda") if estimate.get("moneda") in ("PEN", "USD") else None
    facts = {
        "version": VERSION,
        "valor_estimado": {"monto": float(amount) if amount else None, "moneda": currency if amount else None,
                           "reservado": bool(estimate.get("reservado")) and not amount,
                           "pagina": page(estimate.get("pagina"))},
        "consorcio": {},
        "prestaciones_accesorias": _bool(raw.get("prestaciones_accesorias")),
        "requisitos": [],
    }
    consortium = raw.get("consorcio") if isinstance(raw.get("consorcio"), dict) else {}
    members = _number(consortium.get("max_integrantes"))
    share = _number(consortium.get("participacion_minima"))
    facts["consorcio"] = {"permitido": _bool(consortium.get("permitido")),
                          "max_integrantes": int(members) if members and _written(members, written) else None,
                          "participacion_minima": float(share) if share and _written(share, written) else None}
    for item in raw.get("requisitos") or []:
        if not isinstance(item, dict) or item.get("tipo") not in KINDS:
            continue
        minimum = _number(item.get("monto_minimo"))
        mype = _number(item.get("monto_minimo_mype"))
        times = _number(item.get("veces_valor_estimado"))
        years = _number(item.get("anios"))
        months = _number(item.get("experiencia_meses"))
        count = _number(item.get("cantidad"))
        hours = _number(item.get("capacitacion_horas"))
        # Los meses pueden venir de «2 años» (24 meses): valen si están escritos los meses o los años
        months_ok = months is not None and (_written(months, written) or
                                            (months % 12 == 0 and _written(months / 12, written)))
        clean_item = {
            "tipo": item["tipo"],
            "descripcion": _text(item.get("descripcion"), 300) or _text(item.get("cita"), 300) or "Requisito de las bases",
            "monto_minimo": float(minimum) if minimum and _written(minimum, written) else None,
            "monto_minimo_mype": float(mype) if mype and _written(mype, written) else None,
            "moneda": item.get("moneda") if item.get("moneda") in ("PEN", "USD") else None,
            "veces_valor_estimado": float(times) if times and _written(times, written) else None,
            "anios": int(years) if years and years == int(years) and _written(years, written) else None,
            "cargo": _text(item.get("cargo"), 120),
            "profesion": _text(item.get("profesion"), 120),
            "experiencia_meses": int(months) if months_ok and months == int(months) else None,
            "colegiado": _bool(item.get("colegiado")),
            "cantidad": int(count) if count and count == int(count) and _written(count, written) else None,
            "capacitacion_tema": _text(item.get("capacitacion_tema"), 160) if item["tipo"] == "personal" else None,
            "capacitacion_horas": (int(hours) if item["tipo"] == "personal" and hours and hours == int(hours)
                                   and _written(hours, written) else None),
            "momento": item.get("momento") if item.get("momento") in ("oferta", "contrato") else None,
            "registro_rnp": item.get("registro_rnp") if item.get("registro_rnp") in RNP_REGISTERS else None,
            "documento": _text(item.get("documento"), 200),
            "admite_alquiler": _bool(item.get("admite_alquiler")),
            "pagina": page(item.get("pagina")),
            "cita": _text(item.get("cita"), 400),
        }
        if clean_item["tipo"] == "experiencia" and clean_item["monto_minimo"] and not clean_item["monto_minimo_mype"]:
            clean_item["monto_minimo_mype"] = mype_amount(source, clean_item["monto_minimo"])
        if clean_item["monto_minimo"] is None:
            clean_item["moneda"] = None
        facts["requisitos"].append(clean_item)
    return facts


MYPE_AMOUNT = re.compile(r"(?:micro\s+y\s+pequena\s+empresa|\bmype\b)[\s\S]{0,350}?s\s*/\s*\.?\s*(\d[\d.,\s]*\d)")


def mype_amount(text, general):
    """Monto de experiencia para MYPE escrito en las bases (en la base 1250704: S/ 1,400,000.00 en general y
    S/ 320,000.00 para MYPE). Solo si es menor que el general; si no, None. 23/11/2026."""
    general = Decimal(str(general))
    for match in MYPE_AMOUNT.finditer(plain(text)):
        raw = re.sub(r"\s(?=\d{3}\b)", "", match.group(1)).strip(" .,")
        amount = _number(raw)
        if amount and 0 < amount < general:
            return float(amount)
    return None


def analyze(section, context="", category=None, pages=None, generate=None):
    """Datos de los requisitos (dict de clean) a partir del texto. Lanza llm.ModelError si Gemini falla."""
    from services import llm
    generate = generate or (lambda prompt: llm.generate_json(prompt, model=config.MODEL, cheap=llm.LOW_COST,
                                                              max_tokens=ANALYSIS_MAX_TOKENS))
    raw = generate(build_prompt(section, context, category))
    return clean(raw, section + "\n" + (context or ""), pages)


def stamp(tender, facts):
    """Guarda el análisis en la convocatoria (db.Tender)."""
    tender.bases_facts = facts
    tender.bases_facts_at = datetime.now(timezone.utc) if facts is not None else None
    tender.bases_facts_model = config.MODEL[:80] if facts is not None else None