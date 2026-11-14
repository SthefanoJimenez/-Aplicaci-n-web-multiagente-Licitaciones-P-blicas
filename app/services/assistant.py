"""Asistente de postulación (HU-014): «¿a qué convocatorias puedo postular?».

Responde con datos reales y verificables, no con lo que el modelo recuerde:

1. `load_profile`: lee el perfil guardado (razón social, RUC, rubros, ventas, experiencia y
   personal) desde la base, nunca del mensaje; lee también los documentos de la empresa y el
   consorcio al que pertenece. Solo se pide lo que falta.
2. `resolve_rubros`: convierte el texto libre del perfil o de la consulta en familias UNSPSC
   del catálogo completo (`services.rubros.match`). No hay rubros escritos en el código.
3. `search_tenders`: convocatorias vigentes de esos rubros, con la etapa y el plazo abiertos.
4. `evaluate`: cruza perfil, documentos, consorcio, requisitos leídos de las bases y cronograma,
   y asigna un semáforo (verde, amarillo, rojo o «por verificar»).
5. `answer`: Gemini redacta la explicación, pero el sistema arma las fichas con los códigos,
   fechas y semáforos ya calculados y **valida** que el modelo no cite códigos que no existen.
   Si la validación falla, se responde con el texto determinista.

El semáforo nunca afirma que se cumple un requisito que no se pudo leer en las bases: eso queda
como «por verificar». La norma que se cita es la Ley N.° 32069 y el OECE (services/legal.py).
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from services import accounts, company_documents, consortia, legal, llm, rubros, schedule, tenders

PROMPT_ANSWER = (Path(__file__).resolve().parent.parent / "prompts" / "assistant_answer.txt").read_text(encoding="utf-8")
PROMPT_NOTE = (Path(__file__).resolve().parent.parent / "prompts" / "assistant_note.txt").read_text(encoding="utf-8")
# Separa el resumen que se muestra en el chat del detalle que el widget abre en una ventana aparte.
DETAIL_MARKER = "\n<!--detalle-->\n"

MAX_TENDERS = 8
SOON_DAYS = 5
RUC_RE = re.compile(r"(?<!\d)((?:10|15|17|20)\d{9})(?!\d)")
EXP_AMOUNT = re.compile(
    r"(?:experiencia|facturaci[oó]n|contratos?\s+similares)[^.]{0,260}?S\s*/\s*\.?\s*([\d][\d\s.,]*\d)",
    re.IGNORECASE | re.DOTALL)
OSINERGMIN = re.compile(r"osinergmin|registro\s+de\s+hidrocarburos", re.IGNORECASE)
EXPERIENCE_WORD = re.compile(r"experiencia|facturaci[oó]n|contratos?\s+similares", re.IGNORECASE)
QUERY_AMOUNT_BEFORE = re.compile(
    r"(\d[\d.,\s]*)\s*(mil|millones|millon)?\s*(?:en\s+)?(?:ventas|facturaci[oó]n|experiencia)", re.IGNORECASE)
QUERY_AMOUNT_AFTER = re.compile(
    r"(?:ventas|facturaci[oó]n|experiencia)[^\d]{0,30}(?:de|por|:)?\s*(?:S\s*/\s*\.?)?\s*"
    r"(\d[\d.,\s]*)\s*(mil|millones|millon)?", re.IGNORECASE)
DOCUMENT_INTENT = re.compile(
    r"document|constancia|vencid|qu[eé]\s+me\s+falta|me\s+faltan|faltan", re.IGNORECASE)
GREETING = re.compile(
    r"^\s*(hola|holi|buenas|buenos\s+d[ií]as|buenas\s+tardes|buenas\s+noches|hey|qu[eé]\s+tal|"
    r"gracias|ok|dale)\b[.!¡?\s]*$", re.IGNORECASE)

SEMAPHORES = {
    "verde": ("🟢", "Postulable con lo verificado"),
    "amarillo": ("🟡", "Viable con condiciones o en consorcio"),
    "rojo": ("🔴", "No recomendable por ahora"),
    "gris": ("⚪", "Por verificar en las bases"),
}
BASES_BLANK = {
    "pendiente": "las bases todavía no se han leído en la ingesta",
    "seccion_no_encontrada": "la sección de requisitos no se encontró en las bases",
    "no_legible": "las bases no tienen texto que se pueda leer",
    "error": "no se pudieron descargar las bases en la última actualización",
    None: "la convocatoria no tiene bases publicadas para leer",
}


class AssistantState(TypedDict, total=False):
    query: str
    context: Dict[str, Any]
    profile: Dict[str, Any]
    missing: List[str]
    documents: Dict[str, str]
    document_rows: List[Dict[str, Any]]
    consortium: Optional[Dict[str, Any]]
    rubros: List[Dict[str, Any]]
    tenders: List[Any]
    evaluations: List[Dict[str, Any]]
    answer: str
    status: str
    db_reason: Optional[str]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _as_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        # Fecha sin hora (cronograma de las bases): se ancla al mediodía UTC para que al pasarla
        # a hora de Lima siga siendo el mismo día.
        return datetime.combine(date.fromisoformat(value.strip()), time(12, 0), tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _today(state):
    value = state["context"].get("today") or datetime.now(timezone.utc).date()
    return value.date() if isinstance(value, datetime) else value


def _decimal(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _pen(value):
    return f"S/ {float(value):,.2f}"


def _days_text(moment, today):
    days = (moment.date() - today).days
    if days < 0:
        return "ya venció"
    if days == 0:
        return "vence hoy"
    if days == 1:
        return "vence mañana"
    return f"vence en {days} días"


def _experience_required(blocks):
    """Mayor monto de experiencia/facturación exigido en los fragmentos de las bases, con su página."""
    found = []
    for block in blocks or []:
        text = block.get("text") or ""
        for match in EXP_AMOUNT.finditer(text):
            amount = consortia.parse_pen(match.group(1))
            if amount and amount > 0:
                found.append({"amount": amount, "page": block.get("page"),
                              "excerpt": " ".join(match.group(0).split())[:240]})
    return max(found, key=lambda item: item["amount"]) if found else None


def _mentions_osinergmin(blocks):
    for block in blocks or []:
        if OSINERGMIN.search(block.get("text") or ""):
            return True
    return False


def _documents_state(factory, user_id):
    """Estado de los documentos por tipo: current, expired, future o missing (sin contenido)."""
    inventory = company_documents.evaluation_inventory(factory, user_id)
    state = {}
    for kind, rows in inventory.items():
        if rows["available"]:
            state[kind] = "current"
        elif rows["expired"]:
            state[kind] = "expired"
        elif rows["future"]:
            state[kind] = "future"
        else:
            state[kind] = "missing"
    return state


def _consortium_state(factory, user_id, email):
    """El consorcio del usuario que más facturación similar aporta; None si no tiene."""
    try:
        overview = consortia.overview(factory, user_id, email)
    except Exception:                     # la evaluación no debe romperse por el consorcio
        return None
    best = None
    for row in list(overview.get("led") or []) + list(overview.get("joined") or []):
        detail = consortia.detail(factory, user_id, row["id"])
        if not detail:
            continue
        combined = consortia.combine(detail["members"])
        amount = _decimal(combined["totals"].get("similar_contracts_amount"))
        if best is None or amount > best["amount"]:
            best = {"id": row["id"], "name": row["name"], "amount": amount,
                    "members": len(detail["members"])}
    return best


def _profile_is_empty(profile):
    keys = ("business_activity", "rubros", "monthly_sales", "experience_years",
            "similar_contracts_amount", "specialized_staff")
    return not any(profile.get(key) not in (None, "", 0, Decimal("0")) for key in keys)


def _query_experience(query):
    """Monto de ventas/facturación que el usuario menciona en el chat, si lo menciona.

    Sirve para responder sin obligarlo a llenar el perfil: «tengo 3000 mil ventas en autos»
    aporta S/ 3,000,000 como experiencia declarada en esta conversación.
    """
    text = (query or "").lower()
    candidates = []
    for pattern in (
        QUERY_AMOUNT_BEFORE,
        QUERY_AMOUNT_AFTER,
    ):
        for match in pattern.finditer(text):
            amount = consortia.parse_pen(match.group(1))
            if not amount:
                continue
            unit = (match.group(2) or "").strip()
            if unit.startswith("mil"):
                amount *= 1000
            elif unit.startswith("millon"):
                amount *= 1_000_000
            if amount > 0:
                candidates.append(amount)
    return max(candidates) if candidates else None


def _rnp_declared(query):
    """True/False si el mensaje afirma tener o no tener RNP; None si no dice nada."""
    text = (query or "").lower()
    if re.search(r"no\s+tengo\s+rnp|sin\s+rnp|no\s+cuento\s+con\s+rnp|no\s+estoy\s+inscrit", text):
        return False
    if re.search(r"tengo\s+rnp|con\s+rnp|rnp\s+vigente|inscrit[oa]\s+en\s+el\s+rnp", text):
        return True
    return None


# ---------------------------------------------------------------------------
# Nodos del grafo
# ---------------------------------------------------------------------------
def load_profile_node(state: AssistantState) -> dict:
    """Perfil guardado, documentos y consorcio. Nada de esto bloquea la respuesta."""
    context = state["context"]
    profile = accounts.get_profile(context["factory"], context["user_id"])
    match = RUC_RE.search(state.get("query") or "")
    if match and not profile.get("ruc"):
        # Si escribe su RUC, se guarda como dato opcional; no se pide para responder.
        accounts.save_ruc(context["factory"], context["user_id"], match.group(1))
        profile["ruc"] = match.group(1)
    return {
        "profile": profile,
        "missing": [],
        "documents": _documents_state(context["factory"], context["user_id"]),
        "document_rows": company_documents.list_documents(context["factory"], context["user_id"]),
        "consortium": _consortium_state(context["factory"], context["user_id"], context.get("email")),
    }


def resolve_rubros_node(state: AssistantState) -> dict:
    """Rubros del catálogo que corresponden al perfil o a la consulta. Sin listas fijas."""
    profile, context = state["profile"], state["context"]
    try:
        names_map = tenders.rubro_names()
    except tenders.TendersUnavailable:
        names_map = {}
    found: List[dict] = []
    seen = set()
    for text in (profile.get("rubros"), profile.get("business_activity"), state.get("query")):
        for item in rubros.match(text or "", names_map):
            if item["code"] not in seen:
                seen.add(item["code"])
                found.append(item)
    if not found:
        # Rubro resuelto en un turno anterior de la misma conversación.
        for item in context.get("remembered_rubros") or []:
            code = str(item.get("code") or "")
            if code in names_map and code not in seen:
                seen.add(code)
                found.append({"code": code, "name": rubros.clean_name(code, names_map[code]), "score": 0.0})
    return {"rubros": found, "status": "rubros_ready" if found else "need_rubro"}


def route_after_profile(state: AssistantState) -> str:
    if state.get("status") == "need_rubro":
        return "need_rubro"
    return "search"


def search_tenders_node(state: AssistantState) -> dict:
    """Convocatorias vigentes de los rubros resueltos, sin buena pro y con plazo no vencido."""
    codes = {item["code"] for item in state.get("rubros") or []}
    today = _today(state)
    try:
        available = tenders.open_tenders()
    except tenders.TendersUnavailable as error:
        # Sin base configurada o sin conexión: se responde con un aviso, no con un error 503.
        return {"status": "db_unavailable", "tenders": [], "db_reason": str(error)}

    candidates = []
    for tender in available:
        if tender.stage == schedule.BUENA_PRO:
            continue
        if not (codes & set(tender.rubros or [])):
            continue
        deadline = _as_datetime(tender.deadline_at)
        if deadline and deadline.date() < today:
            continue
        candidates.append(tender)
    candidates.sort(key=lambda tender: _as_datetime(tender.published_at) or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True)
    selected = candidates[:MAX_TENDERS]
    return {"tenders": selected, "status": "tenders_ready" if selected else "no_tenders"}


def _deadlines(tender, today):
    """Próximo hito con fecha del cronograma, del plazo de consultas o de la API."""
    events = []
    if tender.enquiry_end:
        events.append({"label": "Consultas y observaciones", "date": _as_datetime(tender.enquiry_end)})
    for event in tender.schedule or []:
        moment = _as_datetime(event.get("start"))
        if moment:
            events.append({"label": event.get("name") or "Etapa del cronograma", "date": moment})
    if tender.deadline_at:
        events.append({"label": "Presentación de ofertas", "date": _as_datetime(tender.deadline_at)})
    upcoming = sorted((event for event in events if event["date"] and event["date"].date() >= today),
                      key=lambda event: event["date"])
    nxt = upcoming[0] if upcoming else None
    days = (nxt["date"].date() - today).days if nxt else None
    return {"next": nxt, "days": days, "soon": bool(days is not None and days <= SOON_DAYS),
            "has_dates": bool(events)}


def _bases_reason(reqs):
    status = (reqs or {}).get("status")
    return f"No se pudieron verificar los requisitos: {BASES_BLANK.get(status, 'las bases no están disponibles')}."


def _evaluate_tender(tender, reqs, state, today):
    """Semáforo de una convocatoria. Solo afirma lo que se puede verificar."""
    profile = state["profile"]
    documents = state.get("documents") or {}
    consortium = state.get("consortium")
    reasons, failed, verified, consortium_covers = [], False, True, False

    rnp = documents.get("rnp", "missing")
    declared_rnp = _rnp_declared(state.get("query"))
    if declared_rnp is False:
        failed = True
        reasons.append("Indicó que no cuenta con **RNP**: sin inscripción vigente no puede contratar "
                       "con el Estado.")
    elif rnp == "expired":
        failed = True
        reasons.append("Su constancia de **RNP** figura **vencida** en Documentos de mi empresa.")
    elif rnp != "current":
        verified = False
        if declared_rnp is True:
            reasons.append("Indicó que cuenta con **RNP**, pero no hay una constancia vigente cargada.")
        else:
            reasons.append("No tiene una constancia de **RNP** cargada; súbala para acreditarlo.")

    bases_read = bool(reqs and reqs.get("available"))
    requirement = _experience_required(reqs["blocks"]) if bases_read else None
    needs_osinergmin = _mentions_osinergmin(reqs["blocks"]) if bases_read else False
    if not bases_read:
        verified = False
        reasons.append(_bases_reason(reqs))

    own = _decimal(profile.get("similar_contracts_amount"))
    own_source = "perfil"
    if own <= 0:
        declared = _query_experience(state.get("query"))
        if declared:
            own, own_source = declared, "chat"
    if requirement:
        needed, page = requirement["amount"], requirement["page"]
        where = f" (bases, pág. {page})" if page else ""
        if own >= needed:
            origen = "lo que indicó en el chat" if own_source == "chat" else "su perfil"
            reasons.append(f"El monto declarado ({_pen(own)}, según {origen}) cubre los "
                           f"{_pen(needed)} de experiencia que piden las bases{where}.")
        elif consortium and consortium["amount"] >= needed:
            consortium_covers = True
            reasons.append(f"Su empresa sola declara {_pen(own)} y las bases piden {_pen(needed)}{where}; "
                           f"el consorcio «{consortium['name']}» suma {_pen(consortium['amount'])} y sí alcanza.")
        else:
            failed = True
            extra = f"; su consorcio suma {_pen(consortium['amount'])}" if consortium else ""
            origen = "lo que indicó en el chat" if own_source == "chat" else "su perfil"
            reasons.append(f"Las bases piden {_pen(needed)} de experiencia{where} y, según {origen}, "
                           f"declara {_pen(own)}{extra}.")
    elif bases_read:
        verified = False
        if EXPERIENCE_WORD.search(" ".join(block.get("text") or "" for block in reqs["blocks"])):
            reasons.append("La experiencia aparece en las bases, pero no con un monto que se pueda leer; "
                           "confírmela en el capítulo de requisitos.")
        else:
            reasons.append("No se identificó una exigencia de experiencia en la sección leída de las bases; "
                           "confírmela antes de postular.")

    if needs_osinergmin:
        verified = False
        reasons.append("Las bases exigen **Registro de Hidrocarburos de OSINERGMIN** y el perfil no acredita "
                       "ese registro.")

    if failed:
        semaphore = "rojo"
    elif consortium_covers:
        semaphore = "amarillo"
    elif not verified:
        semaphore = "gris"
    else:
        semaphore = "verde"

    deadlines = _deadlines(tender, today)
    schedule_lines = []
    for event in tender.schedule or []:
        start = _as_datetime(event.get("start"))
        if not start:
            continue
        label = event.get("name") or "Etapa del cronograma"
        text = f"{label}: {schedule.day_text(start)}"
        end = _as_datetime(event.get("end"))
        if end and end.date() != start.date():
            text += f"–{schedule.day_text(end)}"
        schedule_lines.append(text)
    stage_label = schedule.STAGE_NAMES.get(tender.stage) or "Sin bases publicadas"
    if deadlines["next"]:
        deadline_text = (f"{deadlines['next']['label']}: "
                         f"{schedule.day_text(deadlines['next']['date'])} ({_days_text(deadlines['next']['date'], today)})")
    else:
        deadline_text = "Sin fecha publicada en las bases ni en la API"

    # El título de la API suele ser la nomenclatura del proceso; la descripción sí dice qué se compra.
    description = " ".join((tender.description or "").split())
    title = description if len(description) >= 25 else " ".join((tender.title or "").split())
    if not title:
        title = "Convocatoria sin título"

    return {
        "code": str(tender.code or ""),
        "title": title,
        "process": tender.title,
        "entity": tender.entity or "Entidad no publicada",
        "department": tender.department,
        "method": tender.method,
        "stage": tender.stage,
        "stage_label": stage_label,
        "deadline_text": deadline_text,
        "deadline_soon": deadlines["soon"],
        "deadline_days": deadlines["days"],
        "schedule_lines": schedule_lines[:6],
        "semaphore": semaphore,
        "semaphore_icon": SEMAPHORES[semaphore][0],
        "semaphore_label": SEMAPHORES[semaphore][1],
        "reasons": reasons,
        "bases_read": bases_read,
        "experience_required": float(requirement["amount"]) if requirement else None,
        "experience_page": requirement["page"] if requirement else None,
        "requires_osinergmin": needs_osinergmin,
    }


def evaluate_node(state: AssistantState) -> dict:
    today = _today(state)
    evaluations = []
    for tender in state.get("tenders") or []:
        try:
            reqs = tenders.requirements_of(tender.code)
        except tenders.TendersUnavailable:
            reqs = None
        evaluations.append(_evaluate_tender(tender, reqs, state, today))
    return {"evaluations": evaluations}


# ---------------------------------------------------------------------------
# Respuesta
# ---------------------------------------------------------------------------
def _company_facts(state):
    profile = state["profile"]
    return {
        "razon_social": profile.get("company_name"),
        "ruc": profile.get("ruc") or "no registrado",
        "rubros_declarados": profile.get("rubros") or None,
        "giro": profile.get("business_activity") or None,
        "ventas_mensuales": float(_decimal(profile.get("monthly_sales"))),
        "experiencia_anios": profile.get("experience_years"),
        "monto_facturado_similar": float(_decimal(profile.get("similar_contracts_amount"))),
        "personal_declarado": profile.get("specialized_staff") or None,
        "documentos": state.get("documents") or {},
        "consorcio": ({"nombre": state["consortium"]["name"],
                       "monto_facturado": float(state["consortium"]["amount"]),
                       "integrantes": state["consortium"]["members"]}
                      if state.get("consortium") else None),
    }


def _facts(state, evaluations):
    return {
        "empresa": _company_facts(state),
        "rubros": [{"codigo": item["code"], "nombre": item["name"]} for item in state.get("rubros") or []],
        "convocatorias": [
            {key: value for key, value in evaluation.items()
             if key not in ("semaphore_icon", "reasons")} | {"motivos": evaluation["reasons"]}
            for evaluation in evaluations
        ],
    }


def _allowed_numbers(facts):
    """Números que el modelo puede escribir: códigos de convocatoria, montos y años del marco legal."""
    allowed = set()
    for convocatoria in facts["convocatorias"]:
        allowed.add(str(convocatoria["code"]))
        if convocatoria.get("experience_required"):
            allowed.add(str(int(convocatoria["experience_required"])))
    empresa = facts["empresa"]
    for key in ("monto_facturado_similar", "ventas_mensuales"):
        allowed.add(str(int(empresa.get(key) or 0)))
    if empresa.get("consorcio"):
        allowed.add(str(int(empresa["consorcio"]["monto_facturado"])))
    allowed.update({"32069", "009", "2025", "2026", "2024", "1", "5", "10", "11", "15", "17", "20"})
    return allowed


def _valid_payload(payload, codes, facts):
    """Acepta la redacción del modelo solo si cita convocatorias de la lista y no inventa códigos."""
    if not isinstance(payload, dict):
        return None
    items = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("codigo") or item.get("code") or "").strip()
        text = str(item.get("texto") or item.get("text") or "").strip()
        if code in codes and text and code not in {row["code"] for row in items}:
            items.append({"code": code, "text": text[:900]})
    if not items:
        return None
    intro = str(payload.get("intro") or "").strip()[:1200]
    cierre = str(payload.get("cierre") or "").strip()[:600]
    blob = " ".join([intro, cierre] + [row["text"] for row in items])
    invented = set(re.findall(r"(?<!\d)(\d{5,8})(?!\d)", blob)) - _allowed_numbers(facts)
    if invented:
        return None
    return {"intro": intro, "items": items, "cierre": cierre}


def _gemini_answer(state, evaluations):
    """Redacción de Gemini sobre los hechos ya calculados. None si falla o inventa."""
    facts = _facts(state, evaluations)
    prompt = PROMPT_ANSWER.replace("{{DATOS}}", json.dumps(facts, ensure_ascii=False, default=str))
    try:
        payload = state["context"]["call_model"](prompt)
    except Exception:                     # cuota, red o JSON inválido: se usa el texto determinista
        return None
    return _valid_payload(payload, {item["code"] for item in evaluations}, facts)


def _render(state, evaluations, payload):
    lines = []
    if payload and payload.get("intro"):
        lines.append(payload["intro"])
    else:
        n = len(evaluations)
        plural = "s" if n != 1 else ""
        verb = "coinciden" if n != 1 else "coincide"
        lines.append(f"Revisé las convocatorias vigentes del OECE para su perfil y encontré "
                     f"**{n}** convocatoria{plural} que {verb} con sus rubros. Estas son, "
                     f"con lo que se pudo verificar en las bases:")
    lines.append("")
    by_code = {row["code"]: row["text"] for row in (payload or {}).get("items", [])}
    counts = {key: sum(1 for item in evaluations if item["semaphore"] == key)
              for key in ("verde", "amarillo", "gris", "rojo")}
    if evaluations:
        lines.append(f"**Resumen:** {counts['verde']} postulable(s) con lo verificado, "
                     f"{counts['amarillo']} viable(s) con condiciones o consorcio, "
                     f"{counts['gris']} por verificar en las bases y "
                     f"{counts['rojo']} no recomendable(s) por ahora.")
        lines.append("")
        lines.append("Marco: **Ley N.° 32069** y **OECE**. El detalle de cada convocatoria está "
                     "en el botón «Ver detalle».")
        lines.append(DETAIL_MARKER)
    for evaluation in evaluations:
        icon = evaluation["semaphore_icon"]
        lines.append(f"#### {icon} Proceso {evaluation['code']}")
        lines.append(f"**{evaluation['title']}**")
        lines.append("")
        lines.append(f"- **Entidad:** {evaluation['entity']}"
                     + (f" ({evaluation['department']})" if evaluation.get("department") else ""))
        if evaluation.get("method"):
            lines.append(f"- **Procedimiento:** {evaluation['method']}")
        lines.append(f"- **Etapa:** {evaluation['stage_label']}")
        lines.append(f"- **Plazo:** {evaluation['deadline_text']}")
        if evaluation.get("schedule_lines"):
            lines.append(f"- **Cronograma:** {' · '.join(evaluation['schedule_lines'])}")
        else:
            lines.append("- **Cronograma:** las bases no publican las fechas; revíselas en el SEACE")
        if evaluation["deadline_soon"]:
            lines.append(f"- **Aviso:** el plazo está por vencer; prepare su oferta o sus consultas.")
        lines.append(f"- **Semáforo:** {evaluation['semaphore_label']}")
        for reason in evaluation["reasons"][:4]:
            lines.append(f"- {reason}")
        lines.append(f"- [Ver ficha, bases y cronograma](/convocatorias/{evaluation['code']})")
        if by_code.get(evaluation["code"]):
            lines.append("")
            lines.append(by_code[evaluation["code"]])
        lines.append("")
    if payload and payload.get("cierre"):
        lines.append(payload["cierre"])
    return "\n".join(lines)


def _with_legal(text):
    """Cierra toda respuesta con la norma vigente y las fuentes oficiales."""
    sources = "\n".join(f"- [{item['label']}]({item['url']})" for item in legal.sources())
    return f"{text}\n\n---\n{legal.CLOSING}\n\n{sources}"


def _polish(state, text):
    """Gemini reescribe un aviso ya calculado. Si falla o inventa cifras, se usa el original."""
    if not text:
        return text
    facts = {"empresa": _company_facts(state),
             "rubros": [{"codigo": item["code"], "nombre": item["name"]} for item in state.get("rubros") or []],
             "estado": state.get("status")}
    prompt = (PROMPT_NOTE
              .replace("{{DATOS}}", json.dumps(facts, ensure_ascii=False, default=str))
              .replace("{{AVISO}}", text))
    try:
        payload = state["context"]["call_model"](prompt)
    except Exception:                     # sin clave, cuota o JSON inválido
        return text
    candidate = str(payload.get("respuesta") or "").strip() if isinstance(payload, dict) else ""
    if not candidate or len(candidate) > 1200:
        return text
    # No se acepta una reescritura que introduzca códigos o montos que no estaban.
    allowed = re.findall(r"(?<!\d)\d{1,4}(?!\d)", text)
    if set(re.findall(r"(?<!\d)(\d{5,8})(?!\d)", candidate)) - set(allowed) - {"32069", "009", "2025", "2026"}:
        return text
    return candidate


def _greeting_answer(state):
    name = (state.get("profile") or {}).get("company_name")
    return ("### Hola" + (f", {name}" if name else "") + "\n\n"
            "Soy el asistente de postulación de LicitaFácil. Reviso el perfil de su empresa, sus "
            "documentos y las convocatorias vigentes del OECE para decirle a cuáles puede postular "
            "sin que lo rechacen.\n\n"
            "Para empezar, cuénteme **qué vende su empresa** (por ejemplo «diésel y gasolina» o "
            "«cemento y acero») o complete sus datos en [Mi empresa](/perfil).")


def _rubro_answer(state):
    profile = state["profile"]
    declared = (profile.get("rubros") or profile.get("business_activity") or "").strip()
    lines = ["### Necesito saber qué vende su empresa", ""]
    if declared:
        lines.append(f"En su perfil figura «{declared}», pero no encontré un rubro del catálogo del OECE "
                     f"que coincida con ese texto.")
    else:
        lines.append("Su perfil todavía no indica qué bienes vende, y sin eso no puedo buscar convocatorias.")
    lines += ["", "Escríbame el producto principal (por ejemplo «diésel y gasolina» o «cemento y acero») "
                  "y lo busco en el catálogo completo; o actualícelo en [Mi empresa](/perfil)."]
    return "\n".join(lines)


def _no_tenders_answer(state):
    names = ", ".join(f"**{item['name']}** ({item['code']})" for item in state.get("rubros") or [])
    return ("### No hay convocatorias vigentes para esos rubros\n\n"
            f"El OECE no publica hoy convocatorias vigentes de {names} que cumplan el filtro "
            f"(etapa abierta y plazo no vencido).\n\n"
            "- Revise el [listado completo de convocatorias](/convocatorias) por si cambió el rubro.\n"
            "- Si su rubro aún no se descarga, [solicítelo en el catálogo](/rubros).\n"
            "- Mientras tanto, puede revisar el [panorama del mercado](/panorama) para ver quién compra "
            "ese bien y a qué precios.")


def _documents_answer(state):
    """Resumen de los documentos de la empresa: cuáles están vigentes, vencidos o faltan."""
    rows = state.get("document_rows") or []
    documents = state.get("documents") or {}
    labels = {"rnp": "RNP", "experiencia": "Experiencia", "personal": "Personal", "otro": "Otros"}
    lines = ["### Los documentos de su empresa", ""]
    pending = []
    for kind, label in labels.items():
        status = documents.get(kind, "missing")
        current = next((row for row in rows if row["document_type"] == kind and row["state"] == status), None)
        if status == "current":
            if current and current.get("expires_on"):
                lines.append(f"- **{label}:** vigente hasta el {schedule.day_text(current['expires_on'])}.")
            else:
                lines.append(f"- **{label}:** cargado, sin vencimiento declarado.")
        elif status == "expired":
            until = schedule.day_text(current["expires_on"]) if current and current.get("expires_on") else "una fecha anterior"
            lines.append(f"- **{label}:** **vencido** el {until}. Vuelva a cargarlo.")
            pending.append(label)
        elif status == "future":
            lines.append(f"- **{label}:** cargado, pero todavía no está vigente según la fecha declarada.")
        else:
            lines.append(f"- **{label}:** no cargado.")
            pending.append(label)
    lines.append("")
    if pending:
        lines.append(f"Le falta cargar o renovar: **{', '.join(pending)}**. Sin el RNP vigente, el asistente "
                     f"marca las convocatorias como no recomendables.")
    else:
        lines.append("No le falta ningún documento de los que el sistema controla. Recuerde revisar también "
                     "los requisitos específicos de cada convocatoria.")
    lines.append("")
    lines.append("Actualícelos en [Documentos de mi empresa](/documentos).")
    return "\n".join(lines)


def _db_answer(state):
    """Aviso honesto cuando la base de convocatorias no está configurada o no responde."""
    if (state.get("db_reason") or "") == "sin_configurar":
        return ("### Falta conectar la base de convocatorias\n\n"
                "Este servidor todavía no tiene configurada la base de convocatorias del OECE "
                "(`DATABASE_URL`), así que no puedo buscar procesos vigentes ni evaluar sus bases. "
                "No voy a inventar convocatorias.\n\n"
                "- Para habilitarlo, configure `DATABASE_URL` y ejecute la ingesta (`python ingest_job.py`).\n"
                "- Mientras tanto, puede revisar el [panorama del mercado](/panorama) con las "
                "adjudicaciones históricas 2023-2025.\n"
                "- [Ver el estado de los datos](/convocatorias/estado)")
    return ("### No puedo consultar las convocatorias ahora\n\n"
            "La base de convocatorias del OECE no respondió en este momento. No voy a inventar "
            "convocatorias: vuelva a intentarlo en unos minutos.\n\n"
            "- [Ver el estado de los datos](/convocatorias/estado)\n"
            "- [Revisar convocatorias vigentes](/convocatorias)")


def answer_node(state: AssistantState) -> dict:
    status = state.get("status")
    query = state.get("query") or ""
    if DOCUMENT_INTENT.search(query):
        return {"answer": _with_legal(_polish(state, _documents_answer(state))), "status": "documents"}
    if GREETING.match(query):
        return {"answer": _with_legal(_polish(state, _greeting_answer(state))), "status": "greeting"}
    if status == "db_unavailable":
        return {"answer": _with_legal(_polish(state, _db_answer(state))), "status": "db_unavailable"}
    if status == "need_rubro":
        return {"answer": _with_legal(_polish(state, _rubro_answer(state))), "status": "need_rubro"}
    if status == "no_tenders":
        return {"answer": _with_legal(_no_tenders_answer(state)), "status": "no_tenders"}

    evaluations = state.get("evaluations") or []
    payload = _gemini_answer(state, evaluations)
    return {"answer": _with_legal(_render(state, evaluations, payload)), "status": "answered"}


# ---------------------------------------------------------------------------
# Grafo
# ---------------------------------------------------------------------------
def build_assistant_graph():
    graph = StateGraph(AssistantState)
    graph.add_node("load_profile", load_profile_node)
    graph.add_node("resolve_rubros", resolve_rubros_node)
    graph.add_node("search_tenders", search_tenders_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("answer", answer_node)

    graph.add_edge(START, "load_profile")
    graph.add_edge("load_profile", "resolve_rubros")
    graph.add_conditional_edges(
        "resolve_rubros", route_after_profile,
        {"need_rubro": "answer", "search": "search_tenders"})
    graph.add_edge("search_tenders", "evaluate")
    graph.add_edge("evaluate", "answer")
    graph.add_edge("answer", END)
    return graph.compile(name="Assistant: Tender Qualification")


ASSISTANT_GRAPH = build_assistant_graph()


def ask_assistant(query, user_id, factory, email=None, history=None, remembered_rubros=None,
                  today=None, call_model=None):
    """Punto de entrada del asistente.

    query: texto del usuario. user_id y factory: la cuenta autenticada y su base de cuentas.
    email: para resolver las invitaciones y los consorcios de la empresa.
    remembered_rubros: rubros resueltos en turnos anteriores (los guarda la sesión de Flask).
    today y call_model: puntos de inyección para las pruebas.
    """
    query = (query or "").strip()
    if not query:
        return {"response": "", "status": "empty", "rubros": [], "missing": []}
    state: AssistantState = {
        "query": query,
        "context": {
            "factory": factory,
            "user_id": user_id,
            "email": email,
            "today": today or datetime.now(timezone.utc),
            "remembered_rubros": remembered_rubros or [],
            "call_model": call_model or llm.generate_json,
        },
        "status": "start",
    }
    result = ASSISTANT_GRAPH.invoke(state)
    return {
        "response": result.get("answer", ""),
        "status": result.get("status", "answered"),
        "rubros": [{"code": item["code"], "name": item["name"]} for item in result.get("rubros") or []],
        "missing": result.get("missing") or [],
        "profile": result.get("profile") or {},
        "detail_count": len(result.get("evaluations") or []),
    }
