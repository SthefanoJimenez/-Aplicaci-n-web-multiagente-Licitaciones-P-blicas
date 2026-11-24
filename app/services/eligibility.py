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

import config
from services import accounts, bases_facts, company_documents, data, db, eligibility_store, guarantees, requirements, schedule, staff, tenders

LABELS = {"verde": "Cumple por sí sola", "amarillo": "Requiere atención", "rojo": "No cumple ni en consorcio"}
REVIEW_OPTIONS = {"pending": "Sin responder", "own": "Cumplo por mí misma",
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


def _offer_day(events):
    days = []
    for event in events or []:
        if not isinstance(event, dict) or event.get("key") != "ofertas":
            continue
        try:
            days.append(date.fromisoformat(str(event.get("end") or event.get("start"))[:10]))
        except (TypeError, ValueError):
            continue
    return max(days) if days else None


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
            result[str(row.code)] = {"id": row.id, "ocid": row.ocid, "code": str(row.code), "title": row.title or row.description,
                "rubros": row.rubros.split() if row.rubros else [],
                "category": tenders.category_of(row), "method": row.method,
                "state": row.state, "stage": row.stage, "deadline_at": row.deadline_at, "published_at": row.published_at,
                "offer_day": _offer_day(row.schedule) if not row.deadline_at else None,
                "currency": row.currency, "estimated_value": row.estimated_value,
                "requirements_status": row.requirements_status, "requirements": entries,
                "bases_url": next((d.url for d in row.documents if d.is_bases), None), "items": [],
                "enquiry_end": row.enquiry_end, "facts": None, "source_file": source_name}
    # Una base antigua puede seguir leyéndose antes de ejecutar la migración aditiva de la ingesta.
    try:
        with factory() as session:
            items = dict(session.execute(select(db.Tender.code, db.Tender.items).where(db.Tender.code.in_(result))).all())
        for code, value in items.items():
            result[str(code)]["items"] = value or []
    except SQLAlchemyError:
        pass
    try:      # requisitos analizados (20/11/2026); una base sin migrar sigue funcionando sin ellos
        with factory() as session:
            facts = dict(session.execute(select(db.Tender.code, db.Tender.bases_facts)
                                         .where(db.Tender.code.in_(result))).all())
        for code, value in facts.items():
            if value and str(code) in result:
                use_facts(result[str(code)], value)
    except SQLAlchemyError:
        pass
    return result


def use_facts(tender, facts):
    """Con los requisitos analizados, cada requisito de la ficha es uno de ellos (no un bloque de página).

    Si el análisis no encontró requisitos pero hay texto leído, se mantienen los bloques de texto.
    """
    tender["facts"] = facts
    items = [item for item in facts.get("requisitos") or [] if isinstance(item, dict)]
    section = " ".join(r.get("text") or "" for r in tender.get("requirements") or [])
    for item in items:
        # Análisis anteriores al 23/11/2026 no guardaban el monto para MYPE: se busca en el texto leído
        if item.get("tipo") == "experiencia" and item.get("monto_minimo") and "monto_minimo_mype" not in item:
            item["monto_minimo_mype"] = bases_facts.mype_amount(section, item["monto_minimo"])
    if not items or tender.get("requirements_status") != "extraido":
        return tender
    entries, seen = [], set()
    for item in items:
        text = item.get("cita") or item.get("descripcion") or ""
        entry = {"text": text, "page": item.get("pagina"), "file_name": tender.get("source_file"),
                 "label": item.get("descripcion"), "fact": item}
        entry["key"] = hashlib.sha256(f"{tender.get('ocid')}|{item.get('tipo')}|{entry['page']}|{text}|"
                                      f"{item.get('descripcion')}".encode()).hexdigest()[:24]
        while entry["key"] in seen:            # dos requisitos con el mismo texto
            entry["key"] = hashlib.sha256(entry["key"].encode()).hexdigest()[:24]
        seen.add(entry["key"])
        entries.append(entry)
    tender["requirements"] = entries
    return tender


def _evaluation_day(tender, today):
    deadline = tender.get("deadline_at")
    if deadline:
        deadline = deadline.replace(tzinfo=timezone.utc) if deadline.tzinfo is None else deadline
        return deadline.astimezone(ZoneInfo("America/Lima")).date()
    return tender.get("offer_day") or today


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


CONSULTING_WORKS = re.compile(r"consultori\w*\s+de\s+obra|supervision\s+de\s+(?:la\s+)?(?:ejecucion\s+de\s+(?:la\s+)?)?obra|"
                              r"expediente\s+tecnico|elaboracion\s+del?\s+(?:estudio|expediente)")


def contract_kind(tender):
    """bienes, servicios, obras o consultoria_obras. La consultoría de obras llega como servicio en OCDS,
    pero tiene su propio registro en el RNP y no tiene la excepción de garantía de 50 UIT (art. 139.a)."""
    category = tender.get("category") or "bienes"
    if category == "servicios" and (CONSULTING_WORKS.search(plain(tender.get("title")))
                                    or "consultoria" in plain(tender.get("method")) and "obra" in plain(tender.get("title"))):
        return "consultoria_obras"
    return category


# Registro del RNP que corresponde a cada objeto (Directiva de procedimientos y trámites ante el RNP, OECE 2025)
REGISTER_FOR_KIND = {"bienes": "bienes", "servicios": "servicios", "obras": "ejecutor_obras",
                     "consultoria_obras": "consultor_obras"}


OK_STATUSES = ("pass", "info", "assumed")


def _ask_hint(tender, instant):
    """Lo que hace un postor cuando a las bases les falta un dato: preguntarlo en consultas y observaciones
    (las bases de la Ley 32069 dan al menos siete días hábiles desde la convocatoria). Solo si el plazo sigue."""
    end = tender.get("enquiry_end")
    if not end or tender.get("stage") not in (None, schedule.ADMINISTRATIVAS):
        return ""
    end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
    if end < instant:
        return ""
    return (" Si lo necesita, puede pedirlo a la entidad en consultas y observaciones hasta el "
            f"{end.astimezone(ZoneInfo('America/Lima')).strftime('%d/%m/%Y')}.")


def _money(value, currency="PEN"):
    return f"{'US$' if currency == 'USD' else 'S/'} {value:,.2f}"


def _in_soles(value, currency):
    """(monto en soles, nota) de un requisito; en dólares, con el tipo de cambio de referencia."""
    if currency != "USD":
        return value, ""
    rate = Decimal(str(config.USD_RATE))
    return (value * rate).quantize(Decimal(".01")), (f" (US$ {value:,.2f} a S/ {rate} por dólar, tipo de cambio de "
                                                     "referencia; las bases indican cuál usar)")


def _fact_status(fact, ctx):
    """(estado, motivo) de un requisito analizado, comparado con los datos de la empresa.

    Estados: pass (cumple con sus datos), assumed (se da por cumplido; se dice por qué), short (no alcanza por sí
    sola: un socio podría aportarlo), fail (no se puede cumplir), pending (falta un dato de la empresa o
    confirmar algo que solo ella sabe), info (las bases no dan el dato).
    """
    profile, kind = ctx["profile"], fact.get("tipo")
    if kind == "rnp":
        register = fact.get("registro_rnp") or REGISTER_FOR_KIND[ctx["kind"]]
        name = accounts.RNP_REGISTERS.get(register, register).lower()
        declared = profile.get("rnp_registers") or []
        if not declared:
            return "pending", f"Indique sus registros vigentes del RNP en Mi empresa (se pide {name})."
        if register in declared:
            return "pass", f"Su perfil declara inscripción vigente como {name}."
        return "pending", (f"Se pide inscripción como {name} y su perfil no la declara. Si ya la tiene, márquela en "
                           "Mi empresa; un socio no sustituye su propia inscripción.")
    if kind == "experiencia":
        status, reason = _experience_status(fact, ctx)
        if status in ("pass", "assumed") and ctx.get("out_of_rubro"):
            # 23/11/2026: el monto alcanza, pero la facturación declarada es de otros rubros
            return "pending", (reason + " Pero la convocatoria no es de sus rubros: confirme que esa facturación es "
                               "de contratos similares a lo que piden las bases.")
        return status, reason
    return _other_fact_status(fact, ctx)


def _company_amount(ctx):
    """(monto facturado en contratos similares que corresponde a esta convocatoria, de qué es, ¿es solo el total?).

    23/11/2026: si Mi empresa dice cuánto es de cada objeto (bienes, servicios, obras), se usa ese; si la empresa
    ofrece un solo objeto, el total; si ofrece varios y no lo separó, el total, pero no basta para dar verde.
    """
    profile, category = ctx["profile"], ctx["category"]
    by_category = {name: decimal(profile.get(field)) for name, field in accounts.SIMILAR_BY_CATEGORY.items()}
    total = decimal(profile.get("similar_contracts_amount"))
    label = accounts.CATEGORY_LABELS.get(category, category).lower()
    amount, how = accounts.similar_for_category(total, by_category, profile.get("categories") or [], category)
    if how == "declarado":
        return amount, f"en {label}", False
    if how == "resto":
        # Ofrece varios objetos y declaró todos menos este: lo de este es lo que queda del total.
        return amount, f"en {label}", "resto"
    return amount, "", bool(amount and how == "sin separar")


def _experience_status(fact, ctx):
    status, reason = _experience_check(fact, ctx)
    if _company_amount(ctx)[2] == "resto":
        label = accounts.CATEGORY_LABELS.get(ctx["category"], ctx["category"]).lower()
        reason += (f" Lo de {label} es lo que queda de su total al restar lo que declaró en los otros objetos "
                   "en Mi empresa.")
    return status, reason


def _experience_check(fact, ctx):
    profile = ctx["profile"]
    actual, where, only_total = _company_amount(ctx)
    only_total = only_total is True
    window = f" en los últimos {fact['anios']} años" if fact.get("anios") else ""
    minimum, note, general = None, "", None
    if fact.get("monto_minimo"):
        minimum, note = _in_soles(decimal(fact["monto_minimo"]), fact.get("moneda"))
        if fact.get("monto_minimo_mype") and profile.get("is_mype"):
            # Las bases fijan un monto menor para MYPE y la empresa lo es (Mi empresa). En consorcio
            # todas deben ser MYPE: ahí se usa el general.
            general = minimum
            minimum, note = _in_soles(decimal(fact["monto_minimo_mype"]), fact.get("moneda"))
            note += " (monto para MYPE)"
    elif fact.get("veces_valor_estimado"):
        times = decimal(fact["veces_valor_estimado"])
        if ctx["amount"] is None:
            return "info", (f"Las bases piden facturar {_times(times)} el valor estimado{window}, y ese monto no se "
                            "conoce. Compárelo con su facturación cuando la entidad lo dé a conocer.")
        minimum = (times * ctx["amount"]).quantize(Decimal(".01"))
        note = f" ({_times(times)} el valor estimado)"
    if actual is None:
        return "pending", "Indique en Mi empresa cuánto ha facturado en contratos similares."
    if minimum is None:
        if actual > 0:
            return "assumed", (f"Las bases no fijan un monto mínimo{window}; se considera su facturación declarada "
                               f"en contratos similares{' ' + where if where else ''} ({_money(actual)}).")
        return "pending", "Las bases piden experiencia en contratos similares y su perfil no declara facturación."
    if actual >= minimum:
        if only_total:
            label = accounts.CATEGORY_LABELS.get(ctx["category"], ctx["category"]).lower()
            return "pending", (f"Su facturación total en contratos similares ({_money(actual)}) alcanza el mínimo "
                               f"de {_money(minimum)}{note}{window}, pero suma todos sus objetos. Indique en Mi "
                               f"empresa cuánto es de {label} para confirmarlo.")
        return "pass", (f"Su facturación en contratos similares{' ' + where if where else ''} ({_money(actual)}) "
                        f"alcanza el mínimo de {_money(minimum)}{note}{window}.")
    shortfall = (f"Su facturación en contratos similares{' ' + where if where else ''} ({_money(actual)}) no alcanza "
                 f"el mínimo de {_money(minimum)}{note}{window}.")
    if ctx["consortium_forbidden"]:
        return "fail", shortfall[:-1] + ", y las bases no admiten consorcio."
    return _with_consortium(ctx, "similar_contracts_amount", general or minimum, shortfall,
                            lambda total: f"con su consorcio «{{name}}» suman {_money(total)}")


def _other_fact_status(fact, ctx):
    profile, kind = ctx["profile"], fact.get("tipo")
    if kind == "experiencia_anios":
        years, actual = fact.get("anios"), profile.get("experience_years")
        if not years:
            return "assumed", "Las bases piden experiencia de la empresa sin un mínimo de años."
        if actual is None:
            return "pending", "Indique en Mi empresa sus años de experiencia."
        if int(actual) >= int(years):
            return "pass", f"Su empresa declara {actual} años de experiencia; las bases piden {years}."
        shortfall = f"Su empresa declara {actual} años de experiencia; las bases piden {years}."
        if ctx["consortium_forbidden"]:
            return "fail", shortfall + " Las bases no admiten consorcio."
        return _with_consortium(ctx, "experience_years", int(years), shortfall,
                                lambda total: f"en su consorcio «{{name}}» la integrante con más experiencia tiene {total} años")
    if kind == "personal":
        return _staff_status(fact, ctx)
    if kind == "equipamiento":
        if fact.get("admite_alquiler"):
            return "assumed", ("Las bases aceptan acreditarlo con equipos alquilados o un compromiso de alquiler: si no "
                               "los tiene, puede alquilarlos.")
        return "pending", "Confirme que cuenta con este equipamiento (propio o como lo admitan las bases)."
    if kind == "habilitacion":
        return _license_status(fact, ctx)
    if kind == "ventas":
        minimum = decimal(fact.get("monto_minimo"))
        monthly = decimal(profile.get("monthly_sales"))
        if minimum is None:
            return "assumed", "Las bases no fijan un monto mínimo de ventas."
        if monthly is None:
            return "pending", "Indique en Mi empresa sus ventas mensuales."
        minimum, note = _in_soles(minimum, fact.get("moneda"))
        yearly = monthly * 12
        if yearly >= minimum:
            return "assumed", (f"Sus ventas mensuales por 12 meses ({_money(yearly)}) superan el mínimo de "
                               f"{_money(minimum)}{note}; confirme el periodo que piden las bases.")
        return ("fail" if ctx["consortium_forbidden"] else "short"), (
            f"Sus ventas mensuales por 12 meses ({_money(yearly)}) no alcanzan el mínimo de {_money(minimum)}{note}.")
    if kind == "documento":
        return "assumed", "Es un formato o declaración que se llena al presentar la oferta."
    return "pending", "Confirme si su empresa cumple este requisito."


def _rubro_label(codes, names):
    """«5019» o «3011 y 7212». Solo códigos: los nombres del catálogo son listas de ejemplos («Ej.: papas fritas,
    mix de bocaditos…») que alargan el aviso sin aclarar (pantalla del 23/11/2026)."""
    codes = list(codes)
    return codes[0] if len(codes) == 1 else ", ".join(codes[:-1]) + " y " + codes[-1]


def _rubro_check(tender_rubros, scope, groups):
    """La convocatoria no es de los rubros de Mi empresa: amarillo, nunca rojo (23/11/2026).

    El rubro declarado no es un requisito para postular; lo que pesa es la experiencia en contratos similares y
    las habilitaciones del rubro. Si una socia de sus consorcios es de ese rubro, se dice cuál.
    """
    names = scope.get("names") or {}
    wanted, own = _rubro_label(tender_rubros, names), _rubro_label(sorted(scope["codes"]), names)
    for group in groups:
        for company_id, codes in (scope.get("partners") or {}).get(group["id"], {}).items():
            if company_id != group["company_id"] and set(codes) & set(tender_rubros):
                partner = next((m.name for m in group["members"] if m.company_id == company_id), "una socia")
                return _check("rubro", "Rubro de la convocatoria", "consortium",
                              f"Es del rubro {wanted} y en Mi empresa declara {own}. {partner}, de su consorcio "
                              f"«{group['name']}», sí es de ese rubro y puede aportar su experiencia.")
    return _check("rubro", "Rubro de la convocatoria", "pending",
                  f"Es del rubro {wanted} y en Mi empresa declara {own}. Puede postular si tiene experiencia en "
                  "contratos similares; si no, en consorcio con una empresa de ese rubro.")


def _times(times):
    """«1 vez», «2 veces», «1.5 veces»."""
    text = format(times.normalize(), "f")
    return f"{text} vez" if times == 1 else f"{text} veces"


def _with_consortium(ctx, key, need, shortfall, how):
    """La empresa sola no alcanza: ¿alcanza con alguno de sus consorcios (Consorcios, HU-012)? 23/11/2026.

    Devuelve («consortium», motivo con el consorcio y quiénes lo integran) o («short», motivo con la sugerencia).
    """
    from services import consortia
    for group in ctx.get("consortia") or []:
        unconfirmed = False
        if key == "similar_contracts_amount":
            # Solo cuenta lo facturado en el objeto de la convocatoria (23/11/2026)
            parts = [consortia.similar_amount(m, ctx["category"]) for m in group["members"]]
            total = sum((amount for amount, _ in parts), Decimal("0"))
            unconfirmed = any(only_total and amount for amount, only_total in parts)
        else:
            total = consortia.combine(group["members"])["totals"][key]
        if total >= need:
            others = ", ".join(m.name for m in group["members"] if m.company_id != group["company_id"])
            text = how(total).replace("{name}", group["name"])
            label = accounts.CATEGORY_LABELS.get(ctx["category"], ctx["category"]).lower()
            check = (f" Confirme cuánto de eso es de {label} en cada empresa (Mi empresa de cada una)."
                     if unconfirmed else "")
            return "consortium", (f"{shortfall} Sí alcanza en consorcio: {text}"
                                  f"{f' (con {others})' if others else ''}.{check}")
    hint = (" Ninguno de sus consorcios alcanza el mínimo." if ctx.get("consortia")
            else " En consorcio se suma la de un socio: puede formarlo en Consorcios.")
    return "short", shortfall + hint


def _partner_staff(ctx, need, count):
    """Una integrante de alguno de sus consorcios tiene a quien cubre el cargo. Texto o None."""
    for group in ctx.get("consortia") or []:
        partners = [m for m in group.get("staff") or [] if m.get("company_id") != group["company_id"]]
        found = staff.match_requirements([dict(need) for _ in range(count)], partners) if partners else []
        if found and not any(f["problem"] for f in found):
            who = ", ".join(f"{f['member']['name']} ({f['member'].get('company_name', 'socio')})" for f in found)
            return f"Lo cubre {who}, de su consorcio «{group['name']}»."
    return None


def _staff_status(fact, ctx):
    key = staff.profession_key(" ".join(filter(None, (fact.get("profesion"), fact.get("cargo")))))
    role = fact.get("cargo") or fact.get("profesion") or "personal clave"
    later = (" Las bases lo piden para firmar el contrato: puede incorporarlo después de ganar."
             if fact.get("momento") == "contrato" else "")
    if key is None:
        if ctx["members"]:
            return "pending", f"Confirme que alguien de su personal cubre el cargo de {role}.{later}"
        return "pending", f"Las bases piden {role}; registre a su personal en Mi empresa › Personal clave.{later}"
    need = {"profession": key, "label": staff.PROFESSIONS.get(key, key.capitalize()),
            "months": fact.get("experiencia_meses"), "licensed": bool(fact.get("colegiado"))}
    count = max(1, int(fact.get("cantidad") or 1))
    if not ctx["members"]:
        return ("assumed" if later else "pending"), (
            f"Las bases piden {role}; registre a su personal en Mi empresa › Personal clave.{later}")
    found = staff.match_requirements([dict(need) for _ in range(count)], ctx["members"])
    problems = [f["problem"] for f in found if f["problem"]]
    if problems:
        if later:
            return "assumed", " ".join(problems) + later
        partner = _partner_staff(ctx, need, count)
        if partner:
            return "consortium", " ".join(problems) + " " + partner
        return "short", " ".join(problems) + (" Puede incorporarlo antes de la oferta o aportarlo en consorcio."
                                              if not ctx["consortium_forbidden"] else "")
    missing = [f"{f['member']['name']}: {', '.join(f['missing'])}" for f in found if f.get("missing")]
    names = ", ".join(f["member"]["name"] for f in found)
    if missing:
        return "pending", f"{names} cubre el cargo de {role}; falta su sustento en Personal clave ({'; '.join(missing)})."
    return "pass", f"{names} cubre el cargo de {role} según lo registrado en Personal clave."


def _license_status(fact, ctx):
    name = fact.get("documento") or fact.get("descripcion") or "la habilitación"
    words = [w for w in re.findall(r"[a-z]{5,}", plain(name))
             if w not in {"registro", "constancia", "certificado", "vigente", "copia", "autorizacion", "licencia"}]
    others = [d for d in ctx["documents"] if d.get("document_type") == "otro"]
    matching = [d for d in others if words and any(w in plain(d.get("filename")) for w in words)]
    current = [d for d in matching
               if company_documents.document_status(SimpleNamespace(**d), ctx["on_date"])[0] == "current"]
    if current:
        return "pass", f"Entre sus documentos está «{current[0]['filename']}», vigente."
    if matching:
        return "pending", f"Su documento «{matching[0]['filename']}» no está vigente en la fecha evaluada."
    return "pending", f"Suba en Mis documentos: {name}."


def _with_review(status, reason, review):
    """La respuesta de la empresa a «¿Cumple este requisito?» manda sobre lo calculado."""
    if review == "no":
        return "fail", "Declara no cumplir este requisito, tampoco mediante consorcio."
    if review == "own" and status != "fail":
        return "pass", "Declara cumplirlo. " + reason
    if review == "consortium" and status != "fail":
        return "consortium", "Lo cumple mediante el consorcio declarado. " + reason
    if review == "correctable":
        return "correctable", "Declara una falta subsanable según las bases; debe corregirse. " + reason
    return status, reason


def _check(key, label, status, reason, **extra):
    return {"key": key, "label": label, "status": status, "reason": reason, **extra}


def evaluate(tender, profile, documents, values=None, today=None, policy=None, members=None, consortia=None,
             scope=None):
    """Función pura: mismo semáforo en listado, ficha y asistente. Recalcula vigencia y fuentes.

    members: personal clave registrado (services/staff.py), para contrastarlo con lo que piden las bases.
    """
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
    if tender.get("stage") == schedule.BUENA_PRO:
        checks.append(_check("etapa", "Etapa del proceso", "fail",
                             "La convocatoria tiene buena pro otorgada; ya no es una oportunidad para presentar ofertas."))
    if tender["state"] != "vigente" or (deadline and deadline < instant) or (tender.get("offer_day") and tender["offer_day"] < today):
        checks.append(_check("vigencia", "Vigencia de la convocatoria", "fail", "La convocatoria ya no acepta ofertas."))
    if not profile.get("business_activity") or not profile.get("rubros"):
        checks.append(_check("perfil", "Perfil de la empresa", "pending", "Complete el giro y los rubros en Mi empresa."))
    category, kind = tender.get("category") or "bienes", contract_kind(tender)
    category_label = accounts.CATEGORY_LABELS.get(category, category).lower()
    off_category = bool(profile.get("categories") and category not in profile["categories"])
    if off_category:
        checks.append(_check("categoria", "Objeto de la convocatoria", "pending",
                             f"Es una convocatoria de {category_label} y su perfil no declara que ofrezca {category_label}."))
    # Rubro (23/11/2026): no impide postular, pero la experiencia debe ser en contratos similares
    out_of_rubro = False
    if scope and scope.get("codes") and tender.get("rubros") and not off_category:
        tender_rubros = [str(r) for r in tender["rubros"]]
        if not set(tender_rubros) & set(scope["codes"]):
            out_of_rubro = True
            checks.append(_rubro_check(tender_rubros, scope, consortia or []))
    register = REGISTER_FOR_KIND[kind]
    analyzed_rnp = any(isinstance(i, dict) and i.get("tipo") == "rnp"
                       for i in (tender.get("facts") or {}).get("requisitos") or [])
    if profile.get("rnp_registers") and register not in profile["rnp_registers"] and not analyzed_rnp:
        checks.append(_check("rnp_registro", "Registro del RNP", "pending",
                             f"El objeto pide inscripción vigente como {accounts.RNP_REGISTERS[register].lower()} en el RNP "
                             "y su perfil no la declara. Un socio del consorcio no sustituye la inscripción propia."))
    amount = decimal(tender.get("estimated_value"))
    amount = amount if amount is not None and amount > 0 else None
    details, amount_source = [], "publicado"
    facts = tender.get("facts") or {}
    in_bases = facts.get("valor_estimado") or {}
    bases_amount = decimal(in_bases.get("monto"))
    if amount is None and bases_amount and in_bases.get("moneda") == "PEN":
        # 20/11/2026: el valor estimado que no trae la fuente, pero sí las bases (Capítulo I)
        amount, amount_source = bases_amount, "indicado en las bases"
        where = f" (página {in_bases['pagina']})" if in_bases.get("pagina") else ""
        checks.append(_check("monto", "Monto de la convocatoria", "pass",
                             f"Se usa el valor estimado que indican las bases{where}."))
    elif amount is None and in_bases.get("reservado"):
        amount_source = "reservado"
        checks.append(_check("monto", "Monto de la convocatoria", "info",
                             "Las bases indican que la entidad no da a conocer el monto a los postores (es reservado). "
                             "Lo que dependa del monto se indica como porcentaje."))
    elif amount is None and bases_amount and in_bases.get("moneda") == "USD":
        amount_source = "en dólares"
        checks.append(_check("monto", "Monto de la convocatoria", "info",
                             f"Las bases indican el valor estimado en dólares (US$ {bases_amount:,.2f}); no se convierte "
                             "para calcular la garantía, que se indica como porcentaje."))
    elif amount is None and category != "bienes":
        # Los precios históricos son de bienes (precio unitario por CUBSO y unidad). En servicios y obras
        # el ítem suele ser «1 servicio»: no hay un precio unitario comparable que multiplicar.
        # 20/11/2026: es un dato que no se publicó, no un incumplimiento: no cambia el color ni se le pide
        # al cliente (no tiene de dónde sacarlo si las bases tampoco lo traen).
        amount_source = "no publicado"
        checks.append(_check("monto", "Monto de la convocatoria", "info",
                             f"La entidad no publicó el valor estimado y no hay precios históricos de {category_label} "
                             "para estimarlo. Lo que dependa del monto se indica como porcentaje." + _ask_hint(tender, instant)))
    elif amount is None:
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
        if amount is None:
            amount_source = "no publicado"
            reason = ("La entidad no publicó el valor estimado y las cantidades de la fuente no alcanzan para estimarlo "
                      "con precios históricos. Lo que dependa del monto se indica como porcentaje." + _ask_hint(tender, instant))
        checks.append(_check("monto", "Monto de la convocatoria", "pass" if amount is not None else "info", reason))
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
    # Régimen (20/11/2026): ya no se le pide al cliente. Las convocatorias que se ingieren son de la Ley 32069;
    # si las bases citan solo la Ley 30225, queda por revisar.
    regime = values.get("regime") if values.get("regime") in ("32069", "30225") else (
        "30225" if "30225" in full_text and "32069" not in full_text else "32069")
    if regime == "30225":
        checks.append(_check("regimen", "Régimen de las bases", "pending", "Las bases mencionan la Ley 30225; confirme el régimen antes de aplicar reglas de la Ley 32069."))
    if not facts.get("requisitos") and re.search(r"\busd\b|us\s*\$|\bdolares\b", full_text):
        checks.append(_check("moneda_requisitos", "Moneda de los requisitos", "pending", "Hay requisitos en otra moneda; falta una conversión sustentada para compararlos con los importes en soles."))
    if facts.get("prestaciones_accesorias") or "garantia" in full_text and "prestaciones accesorias" in full_text:
        values = dict(values, accessory_required=True)
    # MYPE: dato de Mi empresa (20/11/2026); una declaración anterior en la ficha también vale
    mype = bool(values.get("mype") or profile.get("is_mype"))
    guarantee = guarantees.calculate(amount, dict(values, mype=mype), on_date, policy, kind=kind, regime=regime)
    checks.append(_check("garantia", "Garantías", guarantee["status"], guarantee["reason"]))
    if tender.get("requirements_status") != "extraido" or not tender.get("requirements"):
        checks.append(_check("bases", "Requisitos de las bases", "pending",
                             "Las bases no tienen requisitos extraídos completos; revíselas antes de emitir un cumplimiento."))
    consortium_forbidden = (bool(re.search(r"no\s+(?:se\s+)?(?:admite|permite|acepta)[\w\s]{0,30}consorcio", full_text))
                            or (facts.get("consorcio") or {}).get("permitido") is False)
    owned_current = set(inventory["experiencia"]["current"])
    partner_ready = bool(values.get("consortium_confirmed") and not consortium_forbidden and
                         values.get("partner_document") in owned_current)
    context = {"profile": profile, "inventory": inventory, "documents": documents, "members": members or [],
               "amount": amount, "consortium_forbidden": consortium_forbidden, "kind": kind, "on_date": on_date,
               "category": category, "consortia": [] if consortium_forbidden else (consortia or []),
               "out_of_rubro": out_of_rubro}
    for req in tender.get("requirements", []):
        text, key = req["text"], req["key"]
        normalized = plain(text)
        review = values.get("reviews", {}).get(key, "pending")
        if req.get("fact"):
            status, reason = _fact_status(req["fact"], context)
            status, reason = _with_review(status, reason, review)
            checks.append(_check(key, req.get("label") or "Requisito de las bases", status, reason, text=text,
                                 page=req["page"], file_name=req["file_name"], review=review))
            continue
        problems, numeric_failures, doc_types = [], [], set()
        if re.search(r"\brnp\b|registro nacional de proveedores", normalized):
            doc_types.add("rnp")
            # 20/11/2026: el RNP sale de Mi empresa (registros declarados); ya no se pide en cada ficha
            has_rnp = {"yes": True, "no": False}.get(values.get("rnp"), bool(profile.get("rnp_registers")) or None)
            if has_rnp is False:
                problems.append("Declara no contar con el RNP requerido; un socio no sustituye su inscripción.")
                numeric_failures.append("hard")
            elif has_rnp is None:
                problems.append("Indique sus registros vigentes del RNP en Mi empresa.")
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
        # La ficha 1265895 cita un Reglamento Técnico del cemento, no personal técnico.
        # Se conserva el resto del bloque para detectar exigencias reales en esa misma página.
        staff_text = re.sub(r"\breglamentos?\s+tecnicos?\b", "", normalized)
        required = staff.required_staff(staff_text)
        if required:
            doc_types.add("personal")
            if not members and category != "bienes":
                # En servicios y obras el personal clave decide la calificación: se pide el registro detallado.
                labels = ", ".join(r["label"].lower() for r in required)
                problems.append(f"Las bases piden personal clave ({labels}); registre a su personal en Mi empresa › Personal clave.")
            elif members:
                for found in staff.match_requirements(required, members):
                    if found["problem"]:
                        problems.append(found["problem"] + " Puede incorporarlo antes de la oferta o aportarlo en consorcio.")
                        continue
                    # Fichas técnicas y sustentos (20/11/2026): quien cubre el cargo debe tener sus documentos
                    who, role = found["member"]["name"], found["required"]["label"].lower()
                    if found.get("missing"):
                        problems.append(f"{who} cubre el cargo de {role} según lo declarado, pero falta su sustento "
                                        f"en Mi empresa › Personal clave: {', '.join(found['missing'])}.")
                    for detail in found.get("mismatch", []):
                        problems.append(f"Revise los documentos de {who} ({role}): {detail}")
        if re.search(r"\b(?:personal|" + jobs + r")\b", staff_text):
            doc_types.add("personal")
            if not str(profile.get("specialized_staff") or "").strip() and not members:
                problems.append("El perfil no declara personal especializado.")
            counts = {"un":1,"uno":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,"seis":6,"siete":7,"ocho":8,"nueve":9,"diez":10}
            count_text = re.sub(r"\b(" + "|".join(counts) + r")\s+(?=" + jobs + ")", lambda m:str(counts[m.group(1)])+" ", staff_text)
            count = re.search(r"(\d+)\s*\)?\s*(?:" + jobs + ")", count_text)
            declared = values.get("staff_count") if values.get("staff_count") is not None else (len(members) if members else None)
            if count and (declared is None or int(declared) < int(count.group(1))):
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
    # «info»: dato que la entidad no publicó (monto). Se muestra, pero no cambia el color (20/11/2026).
    # «assumed»: se da por cumplido con lo que dicen las bases (un anexo que se llena, un equipo que se puede
    # alquilar); tampoco cambia el color, pero se dice qué se supuso.
    color = ("rojo" if any(c["status"] == "fail" for c in checks)
             else "amarillo" if any(c["status"] not in OK_STATUSES for c in checks) else "verde")
    decisive = ([c["reason"] for c in checks if c["status"] == "fail"]
                or [c["reason"] for c in checks if c["status"] not in OK_STATUSES])
    return {"code": tender["code"], "title": tender["title"], "color": color, "label": LABELS[color],
            "category": category, "kind": kind,
            "company": {"name": profile.get("company_name", ""), "ruc": profile.get("ruc", "")},
            "reason": decisive[0] if decisive else (
            "Cumple los requisitos con los datos de su empresa; revise lo que se dio por cumplido."
            if any(c["status"] == "assumed" for c in checks) else
            "Todos los requisitos revisados y los controles disponen de sustento declarado compatible."),
        "analyzed": bool(facts.get("requisitos")),
            "checks": checks, "amount": amount, "amount_source": amount_source, "estimate": details,
            "guarantee": guarantee, "inventory": inventory, "on_date": on_date,
            "date_basis": "fecha límite de la convocatoria" if tender.get("deadline_at") else "fecha de presentación de ofertas del cronograma de las bases" if tender.get("offer_day") else "fecha de hoy; la fuente no publica fecha límite",
            "bases_url": tender.get("bases_url"), "requirements": tender.get("requirements", []),
            "item_count": len(tender.get("items") or [])}


def evaluate_many(account_factory, user_id, codes, today=None):
    profile = accounts.get_profile(account_factory, user_id)
    documents = company_documents.list_documents(account_factory, user_id)
    saved = eligibility_store.load_inputs(account_factory, user_id, [str(c) for c in codes])
    members = staff.list_staff(account_factory, user_id, on_date=today)
    groups = company_consortia(account_factory, user_id, today)
    contexts = load_tenders(codes)
    scope = rubro_scope(profile, groups)
    return {code: evaluate(tender, profile, documents, saved.get(code), today, members=members, consortia=groups,
                           scope=scope)
            for code, tender in contexts.items()}


def rubro_scope(profile, groups=()):
    """Rubros (familias de 4 dígitos) de Mi empresa y de las socias de sus consorcios, con sus nombres.
    Los mismos que usa «Solo a las que puedo postular» (services/matching.company_scope)."""
    from services import matching
    try:
        names = tenders.rubro_names()
    except tenders.TendersUnavailable:
        names = {}
    codes = {item["code"] for item in matching.company_scope(profile, names)["rubros"]}
    partners = {group["id"]: {company_id: {item["code"] for item in matching.company_scope(text, names)["rubros"]}
                              for company_id, text in (group.get("rubros") or {}).items()}
                for group in groups}
    return {"codes": codes, "names": names, "partners": partners}


def company_consortia(account_factory, user_id, today=None):
    """Consorcios de la empresa (los que lidera y a los que se unió), con sus integrantes que aceptaron y el
    personal clave de cada una (23/11/2026). Si la base de cuentas no tiene esas tablas, lista vacía."""
    from services import consortia
    try:
        return consortia.with_staff(account_factory, user_id, today)
    except SQLAlchemyError:
        return []


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