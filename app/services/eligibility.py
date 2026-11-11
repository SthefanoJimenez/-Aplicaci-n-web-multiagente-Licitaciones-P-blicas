"""HU-010: semáforo determinista con datos privados y requisitos identificables de las bases.

No inventa requisitos ni páginas. Los requisitos que no pueden comprobarse con seguridad
quedan por revisar. La confirmación del usuario es declarativa, no una validación de autenticidad.
"""
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
import unicodedata
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from services import accounts, company_documents, data, db, eligibility_store, guarantees, requirements, tenders

LABELS = {"verde": "Cumple por sí sola", "amarillo": "Requiere atención", "rojo": "No cumple ni en consorcio"}
REVIEW_OPTIONS = {"pending": "Por revisar", "own": "Cumplo por mí misma",
                  "consortium": "Cumplo solo en consorcio", "correctable": "Falta un requisito subsanable según las bases",
                  "no": "No cumplo ni en consorcio"}


def plain(text):
    return "".join(c for c in unicodedata.normalize("NFD", str(text or "").lower())
                   if unicodedata.category(c) != "Mn")


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result <= Decimal("99999999999999.9999") else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _source_number(value):
    """Importes explícitos de las bases; admite 150,000.00 y 150.000,00."""
    value = value.strip().rstrip("., ").replace(" ", "")
    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".") if value.rfind(",") > value.rfind(".") else value.replace(",", "")
    elif "," in value or "." in value:
        separator = "," if "," in value else "."
        tail = value.rsplit(separator, 1)[1]
        value = value.replace(separator, "") if len(tail) == 3 else value.replace(separator, ".")
    return decimal(value)


def _unit(value):
    unit = plain(value).strip()
    return {"niu": "unidad", "un": "unidad", "unidades": "unidad", "lt": "litro", "litros": "litro",
            "kg": "kilogramo", "kilogramos": "kilogramo", "gli": "galon", "galones": "galon"}.get(unit, unit)


def historical_estimate(items):
    """Cantidad × mediana de precios históricos del CUBSO exacto y la misma unidad; suma todos los ítems."""
    total, details = Decimal("0"), []
    if not items:
        return None, [], "Falta el monto: no hay cantidad y CUBSO confirmados."
    for item in items:
        quantity = decimal(item.get("quantity"))
        classifications = item.get("classifications") or []
        codes = {str(c.get("id", "")) for c in classifications if "cubso" in plain(c.get("scheme"))}
        code = item.get("catalog_code") or (next(iter(codes)) if len(codes) == 1 else None)
        try:
            good = data.GOODS.get(int(code))
        except (TypeError, ValueError):
            good = None
        raw_unit = item.get("unit") or {}
        unit = (raw_unit.get("name") or raw_unit.get("id")) if isinstance(raw_unit, dict) else str(raw_unit)
        if quantity is None or quantity <= 0 or not good or not unit or _unit(unit) != _unit(good["unit"]):
            return None, [], "Falta el monto: cantidad, CUBSO exacto o unidad compatibles no disponibles para todos los ítems."
        rows = data.AWARDS[(data.AWARDS["codigoitem"] == good["code"]) &
                           (data.AWARDS["unidad_medida"] == good["unit"])]
        prices = sorted(value for value in (decimal(v) for v in rows["precio_unitario"]) if value is not None and value > 0)
        if len(prices) < 5:
            return None, [], "Falta el monto: no hay al menos cinco precios históricos comparables."
        middle = len(prices) // 2
        median = prices[middle] if len(prices) % 2 else (prices[middle - 1] + prices[middle]) / 2
        amount = quantity * median
        total += amount
        details.append({"code": good["code"], "name": good["name"], "unit": good["unit"],
                        "quantity": quantity, "median": median, "amount": amount.quantize(Decimal(".01")),
                        "n": len(prices), "year_from": int(rows["anio"].min()), "year_to": int(rows["anio"].max())})
    return total.quantize(Decimal(".01")), details, "Estimación histórica; no es el valor publicado ni el monto contractual."


def load_tenders(codes):
    """Una lectura por lote; los PDF de las empresas nunca se cargan en esta base pública."""
    factory = tenders._session_factory()
    with factory() as session:
        rows = session.scalars(select(db.Tender).where(db.Tender.code.in_([str(c) for c in codes])).options(
            selectinload(db.Tender.documents), selectinload(db.Tender.requirements), selectinload(db.Tender.chunks))).all()
        result = {}
        for row in rows:
            blocks = requirements.merge_chunks([(c.page, c.text) for c in sorted(row.chunks, key=lambda c: c.chunk_index)])
            source_name = row.chunks[0].file_name if row.chunks else None
            entries = [{"text": block.text, "page": block.page, "file_name": source_name} for block in blocks]
            if not entries:
                entries = [{"text": req.text, "page": req.page, "file_name": None} for req in row.requirements]
            for entry in entries:
                entry["key"] = hashlib.sha256(f"{row.ocid}|{entry['page']}|{entry['file_name']}|{entry['text']}".encode()).hexdigest()[:24]
            result[str(row.code)] = {"id": row.id, "code": str(row.code), "title": row.title or row.description,
                "state": row.state, "deadline_at": row.deadline_at, "published_at": row.published_at,
                "currency": row.currency, "estimated_value": row.estimated_value,
                "requirements_status": row.requirements_status, "requirements": entries,
                "bases_url": next((d.url for d in row.documents if d.is_bases), None), "items": []}
    # Una base antigua puede seguir leyéndose antes de ejecutar la migración aditiva de la ingesta.
    try:
        with factory() as session:
            items = dict(session.execute(select(db.Tender.code, db.Tender.items).where(db.Tender.code.in_(result))).all())
        for code, value in items.items():
            result[str(code)]["items"] = value or []
    except SQLAlchemyError:
        pass
    return result


def _evaluation_day(tender, today):
    deadline = tender.get("deadline_at")
    if deadline:
        deadline = deadline.replace(tzinfo=timezone.utc) if deadline.tzinfo is None else deadline
        return deadline.astimezone(ZoneInfo("America/Lima")).date()
    return today


def _minimum(text, topic, amount=None):
    normalized = plain(text)
    if "mype" in normalized and len(re.findall(r"s\s*/\s*\d", normalized)) > 1:
        return None  # La alternativa aplicable se revisa en las bases, no se elige por posición.
    normalized = re.sub(r"(?<=\d)[\s\u00a0]+(?=\d{3}(?:\D|$))", "", normalized)
    number = r"([0-9]+(?:[.,][0-9]+)*)(?:\s+(mil|millon|millones)\b)?"
    snippets = re.findall(r"(?:" + topic + r")[\s\S]{0,240}?s\s*/\s*" + number, normalized)
    snippets += re.findall(r"(?:" + topic + r")[\s\S]{0,240}?" + number + r"\s+(?:de\s+)?soles\b", normalized)
    values = set()
    for raw, unit in snippets:
        value = _source_number(raw)
        if value is not None:
            values.add(value * (Decimal("1000") if unit == "mil" else Decimal("1000000") if unit else Decimal("1")))
    if len(values) == 1:
        return values.pop()
    multiplier = re.search(r"(?:" + topic + r")[\s\S]{0,120}?(\d+(?:[.,]\d+)?)\s*veces?\s+(?:el\s+)?(?:monto|valor)", normalized)
    if not values and multiplier and amount is not None:
        return _source_number(multiplier.group(1)) * amount
    return None


def _check(key, label, status, reason, **extra):
    return {"key": key, "label": label, "status": status, "reason": reason, **extra}


def evaluate(tender, profile, documents, values=None, today=None, policy=None):
    """Función pura: mismo semáforo en listado, ficha y asistente. Recalcula vigencia y fuentes."""
    instant = datetime.now(timezone.utc) if today is None else datetime.combine(today, time.min, tzinfo=ZoneInfo("America/Lima"))
    values, today = values or {}, today or company_documents.today()
    on_date = _evaluation_day(tender, today)
    inventory = {kind: {"current": [], "expired": [], "future": []} for kind in company_documents.DOCUMENT_TYPES}
    for doc in documents:
        state, _ = company_documents.document_status(SimpleNamespace(**doc), on_date)
        inventory[doc["document_type"]][state].append(doc["id"])
    checks = []
    deadline = tender.get("deadline_at")
    if deadline and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if tender["state"] != "vigente" or (deadline and deadline < instant):
        checks.append(_check("vigencia", "Vigencia de la convocatoria", "fail", "La convocatoria ya no acepta ofertas."))
    if not profile.get("business_activity") or not profile.get("rubros"):
        checks.append(_check("perfil", "Perfil de la empresa", "pending", "Complete el giro y los rubros en Mi empresa."))
    amount = decimal(tender.get("estimated_value"))
    amount = amount if amount is not None and amount > 0 else None
    details, amount_source = [], "publicado"
    if amount is None:
        items = tender.get("items") or []
        if values.get("quantity") and values.get("catalog_code") and values.get("whole_contract") and len(items) <= 1:
            item = dict(items[0]) if items else {}
            source_codes = {str(c.get("id")) for c in item.get("classifications", []) if "cubso" in plain(c.get("scheme"))}
            code = next(iter(source_codes)) if len(source_codes) == 1 else str(values["catalog_code"])
            good = data.GOODS.get(int(code)) if code.isdigit() else None
            if decimal(item.get("quantity")) in (None, Decimal("0")):
                item["quantity"] = values["quantity"]
            item["catalog_code"] = code
            raw_unit = item.get("unit")
            if not raw_unit or isinstance(raw_unit, dict) and not (raw_unit.get("name") or raw_unit.get("id")):
                item["unit"] = good["unit"] if good else None
            items = [item] if good else []
            amount_source = "estimado con datos de la fuente y datos complementarios declarados"
        else:
            amount_source = "estimado con cantidades OCDS"
        amount, details, reason = historical_estimate(items)
        checks.append(_check("monto", "Monto de la convocatoria", "pass" if amount is not None else "pending", reason))
    else:
        checks.append(_check("monto", "Monto de la convocatoria", "pass", "Se usa el valor publicado por la fuente."))
    if amount is not None and tender.get("currency") not in (None, "PEN"):
        checks.append(_check("moneda", "Moneda", "pending", "La moneda de la convocatoria no es PEN; no se convierten monedas automáticamente."))
        amount = None
    if amount is not None and tender.get("currency") is None:
        if amount_source == "publicado":
            checks.append(_check("moneda", "Moneda", "pending", "La fuente no indica la moneda del valor publicado."))
            amount = None
    full_text = plain(" ".join(r["text"] for r in tender.get("requirements", [])))
    if "30225" in full_text and values.get("regime") == "32069":
        checks.append(_check("regimen", "Régimen de las bases", "pending", "Las bases mencionan la Ley 30225; confirme el régimen antes de aplicar reglas de la Ley 32069."))
    if re.search(r"\busd\b|us\s*\$|\bdolares\b", full_text):
        checks.append(_check("moneda_requisitos", "Moneda de los requisitos", "pending", "Hay requisitos en otra moneda; falta una conversión sustentada para compararlos con los importes en soles."))
    if "garantia" in full_text and "prestaciones accesorias" in full_text:
        values = dict(values, accessory_required=True)
    guarantee = guarantees.calculate(amount, values, on_date, policy)
    checks.append(_check("garantia", "Garantías", guarantee["status"], guarantee["reason"]))
    if tender.get("requirements_status") != "extraido" or not tender.get("requirements"):
        checks.append(_check("bases", "Requisitos de las bases", "pending",
                             "Las bases no tienen requisitos extraídos completos; revíselas antes de emitir un cumplimiento."))
    consortium_forbidden = bool(re.search(r"no\s+(?:se\s+)?(?:admite|permite|acepta)[\w\s]{0,30}consorcio", full_text))
    owned_current = set(inventory["experiencia"]["current"])
    partner_ready = bool(values.get("consortium_confirmed") and not consortium_forbidden and
                         values.get("partner_document") in owned_current)
    for req in tender.get("requirements", []):
        text, key = req["text"], req["key"]
        normalized = plain(text)
        review = values.get("reviews", {}).get(key, "pending")
        problems, numeric_failures, doc_types = [], [], set()
        if re.search(r"\brnp\b|registro nacional de proveedores", normalized):
            doc_types.add("rnp")
            if values.get("rnp") == "no":
                problems.append("Declara no contar con el RNP requerido; un socio no sustituye su inscripción.")
                numeric_failures.append("hard")
            elif values.get("rnp") != "yes":
                problems.append("Falta declarar la situación del RNP.")
        if "experiencia" in normalized or "facturacion" in normalized:
            doc_types.add("experiencia")
            minimum = _minimum(text, "experiencia|facturacion", amount)
            actual = decimal(profile.get("similar_contracts_amount"))
            if minimum is None and re.search(r"(?:experiencia|facturacion)[\s\S]{0,240}(?:s\s*/|soles|veces)", normalized):
                problems.append("El umbral de experiencia o la alternativa aplicable no se pudo interpretar con seguridad; revise las bases y su extracción.")
            if minimum is not None:
                if actual is None:
                    problems.append("Falta el monto facturado en contratos similares del perfil.")
                elif actual < minimum:
                    partner_amount = decimal(values.get("partner_experience")) or Decimal("0")
                    if partner_ready and actual + partner_amount >= minimum:
                        problems.append(f"La experiencia propia ({actual}) no alcanza {minimum}; solo alcanza con el aporte declarado del socio.")
                        numeric_failures.append("consortium")
                    else:
                        problems.append(f"La experiencia propia ({actual}) es menor al mínimo de las bases ({minimum}).")
                        numeric_failures.append("hard" if consortium_forbidden or partner_ready else "unknown")
            years = re.search(r"experiencia[\s\S]{0,45}?(\d+)\s*anos", normalized)
            if years and profile.get("experience_years") is not None and int(profile["experience_years"]) < int(years.group(1)):
                problems.append("Los años de experiencia declarados no alcanzan lo exigido; revise si un aporte es admisible.")
                numeric_failures.append("hard" if consortium_forbidden else "unknown")
        if "ventas" in normalized:
            doc_types.add("experiencia")
            minimum = _minimum(text, "ventas")
            if minimum is None and re.search(r"ventas[\s\S]{0,240}(?:s\s*/|soles|veces)", normalized):
                problems.append("El umbral de ventas no se pudo interpretar con seguridad; revise las bases y su extracción.")
            if "mensual" in normalized and minimum is not None:
                actual = decimal(profile.get("monthly_sales"))
                if actual is None or actual < minimum:
                    problems.append("Las ventas mensuales del perfil no acreditan el mínimo de las bases.")
                    numeric_failures.append("hard" if consortium_forbidden else "unknown")
            elif review != "own":
                problems.append("Revise el periodo y los importes de ventas exigidos; no se convierten ventas mensuales en anuales.")
        jobs = "tecnicos?|profesional(?:es)?|especialistas?|ingenieros?|conductor(?:es)?|operarios?"
        if re.search(r"\bpersonal\b|" + jobs, normalized):
            doc_types.add("personal")
            if not str(profile.get("specialized_staff") or "").strip():
                problems.append("El perfil no declara personal especializado.")
            counts = {"un":1,"uno":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,"seis":6,"siete":7,"ocho":8,"nueve":9,"diez":10}
            count_text = re.sub(r"\b(" + "|".join(counts) + r")\s+(?=" + jobs + ")", lambda m:str(counts[m.group(1)])+" ", normalized)
            count = re.search(r"(\d+)\s*\)?\s*(?:" + jobs + ")", count_text)
            if count and (values.get("staff_count") is None or int(values["staff_count"]) < int(count.group(1))):
                problems.append("La cantidad de personal declarada no acredita el mínimo exigido; revise también su especialidad.")
        if re.search(r"osinergmin|hidrocarburos|licencia|autorizacion|certificad", normalized):
            doc_types.add("otro")
        for kind in sorted(doc_types):
            if not inventory[kind]["current"]:
                detail = "vencidos" if inventory[kind]["expired"] else "aún no vigentes" if inventory[kind]["future"] else "ausentes"
                problems.append(f"Documentos de {company_documents.DOCUMENT_TYPES[kind]} {detail} en la fecha evaluada ({on_date}).")
        if review == "no":
            status, reason = "fail", "Declara no cumplir este requisito, tampoco mediante consorcio."
        elif "hard" in numeric_failures:
            status, reason = "fail", " ".join(problems)
        elif review == "consortium" and partner_ready and (not problems or numeric_failures == ["consortium"] and len(problems) == 1):
            status, reason = "consortium", "Cumplimiento solo mediante el consorcio declarado, sujeto a las reglas de participación de las bases. " + " ".join(problems)
        elif review == "correctable":
            status, reason = "correctable", "Declara una falta subsanable según las bases; debe corregirse. " + " ".join(problems)
        elif problems:
            status, reason = "pending", " ".join(problems)
        elif review == "own":
            status, reason = "pass", "Datos y vigencia compatibles; la empresa declara haber contrastado el contenido con este requisito."
        elif review == "consortium":
            status, reason = "pending", "Falta confirmar las reglas del consorcio y aportar un documento vigente del socio."
        else:
            status, reason = "pending", "Revise este requisito y confirme su cumplimiento frente a sus documentos; el tipo de PDF no prueba su contenido."
        checks.append(_check(key, "Requisito de las bases", status, reason, text=text, page=req["page"],
                             file_name=req["file_name"], review=review))
    color = "rojo" if any(c["status"] == "fail" for c in checks) else "amarillo" if any(c["status"] != "pass" for c in checks) else "verde"
    decisive = [c["reason"] for c in checks if c["status"] == "fail"] or [c["reason"] for c in checks if c["status"] != "pass"]
    return {"code": tender["code"], "title": tender["title"], "color": color, "label": LABELS[color],
            "reason": decisive[0] if decisive else "Todos los requisitos revisados y los controles disponen de sustento declarado compatible.",
            "checks": checks, "amount": amount, "amount_source": amount_source, "estimate": details,
            "guarantee": guarantee, "inventory": inventory, "on_date": on_date,
            "date_basis": "fecha límite de la convocatoria" if tender.get("deadline_at") else "fecha de hoy; la fuente no publica fecha límite",
            "bases_url": tender.get("bases_url"), "requirements": tender.get("requirements", []),
            "item_count": len(tender.get("items") or [])}


def evaluate_many(account_factory, user_id, codes, today=None):
    profile = accounts.get_profile(account_factory, user_id)
    documents = company_documents.list_documents(account_factory, user_id)
    saved = eligibility_store.load_inputs(account_factory, user_id, [str(c) for c in codes])
    contexts = load_tenders(codes)
    return {code: evaluate(tender, profile, documents, saved.get(code), today) for code, tender in contexts.items()}


def parse_form(form, requirement_keys, owned_documents):
    """Solo entradas declarativas permitidas; no acepta empresa, reglas ni importes de bases del cliente."""
    values, errors = {}, []
    for key in ("guarantee_capacity", "accessory_amount", "advance_amount", "partner_experience", "quantity"):
        raw = str(form.get(key, "")).strip()
        if raw:
            number = decimal(raw) if re.fullmatch(r"\d{1,14}(?:\.\d{1,4})?", raw) else None
            if number is None:
                errors.append("Use importes y cantidades positivos, con punto decimal y sin separadores de miles.")
            else:
                values[key] = str(number)
    count = str(form.get("staff_count", "")).strip()
    if count:
        if not re.fullmatch(r"\d{1,6}", count):
            errors.append("Indique una cantidad entera de personal entre 0 y 999999.")
        else:
            values["staff_count"] = int(count)
    code = str(form.get("catalog_code", "")).strip()
    if code:
        if not code.isdigit() or int(code) not in data.GOODS:
            errors.append("Seleccione un código CUBSO existente en el catálogo histórico.")
        else:
            values["catalog_code"] = code
    if bool(values.get("quantity")) != bool(values.get("catalog_code")):
        errors.append("Para estimar, indique juntos la cantidad y el CUBSO exacto.")
    for key in ("mype", "whole_contract", "consortium_confirmed", "accessory_required"):
        values[key] = form.get(key) == "1"
    for key, allowed in (("regime", {"", "32069", "30225"}), ("rnp", {"", "yes", "no"}),
                         ("guarantee_method", {"", "retention", "coverage"})):
        value = form.get(key, "")
        if value not in allowed:
            errors.append("Revise las opciones seleccionadas.")
        values[key] = value if value in allowed else ""
    partner = str(form.get("partner_document", "")).strip()
    if partner:
        if not partner.isdigit() or int(partner) not in {d["id"] for d in owned_documents}:
            errors.append("El documento del socio debe estar guardado en su propia empresa.")
        else:
            values["partner_document"] = int(partner)
    values["reviews"] = {}
    for key in requirement_keys:
        value = form.get("review_" + key, "pending")
        if value not in REVIEW_OPTIONS:
            errors.append("Revise la respuesta de cada requisito.")
        else:
            values["reviews"][key] = value
    return values, list(dict.fromkeys(errors))
