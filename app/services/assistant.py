"""Asistente para postulaciones a convocatorias con LangGraph (HU-010 / HU-014).

Ayuda a un empresario proveedor (especialmente a quienes nunca han vendido al Estado)
a saber a qué convocatorias vigentes puede postular según su perfil, sus documentos,
el consorcio (si existe) y los requisitos extraídos de las bases.

Estructura del grafo:
1. start_node: normaliza estado, extrae o conserva el perfil.
2. check_profile_node: valida si faltan datos esenciales para evaluar (ej. rubro/actividad o experiencia).
   Si faltan datos, bifurca a request_data_node (pide el dato en lugar de suponerlo).
3. search_tenders_node: busca en la base de datos de convocatorias vigentes reales que coincidan.
4. evaluate_tenders_node: contrasta requisitos de las bases (RAG / bases_reader) contra perfil + consorcio.
   Calcula semáforo:
   - verde: cumple habilitación, experiencia y requisitos.
   - amarillo: viable con consorcio o requisitos subsanables.
   - rojo: no cumple requisitos excluyentes (falta habilitación clave o experiencia insuficiente sin consorcio).
5. generate_response_node: redacta la respuesta citando expresamente el código real, el semáforo,
   la explicación y la página exacta de las bases.
"""
from typing import Any, Dict, List, Optional, TypedDict
import re
import unicodedata

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from services import db, formatting, rag, tenders


class Message(TypedDict):
    role: str   # "user" | "assistant"
    content: str


# Frases de ventas al Estado. La negación se evalúa primero porque «nunca le he vendido
# al Estado» contiene a su vez «he vendido al estado».
_NEGATIVE_SALES = re.compile(
    r"\b(?:nunca|jam[aá]s|no)\b[^.;\n]{0,40}?\b(?:vendid[oa]s?|vendo|vende|ventas?)\b"
)
_POSITIVE_SALES = re.compile(
    r"\b(?:he|hemos|ya|vengo)\s+vendid[oa]s?\b|\bvendo al estado\b|\bproveedor(?: habitual)?\b"
)


class AssistantState(TypedDict, total=False):
    query: str
    messages: List[Dict[str, str]]
    profile: Dict[str, Any]
    missing_data: Optional[str]
    tenders_found: List[Dict[str, Any]]
    evaluations: List[Dict[str, Any]]
    response: str
    status: str  # "need_data" | "answered" | "no_tenders"


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


# ---------------------------------------------------------------------------
# Nodos del grafo
# ---------------------------------------------------------------------------
def start_node(state: AssistantState) -> Dict[str, Any]:
    query = state.get("query", "").strip()
    profile = extract_profile_updates(query, state.get("profile", {}))
    return {"profile": profile}


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


def route_after_profile_check(state: AssistantState) -> str:
    if state.get("status") == "general_guidance":
        return "preparation_guide"
    if state.get("status") == "need_data":
        return "request_data"
    return "search_tenders"


def request_data_node(state: AssistantState) -> Dict[str, Any]:
    missing = state.get("missing_data", "Por favor indica qué bienes ofrece tu empresa.")
    return {"response": missing}


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


def search_tenders_node(state: AssistantState) -> Dict[str, Any]:
    """Busca convocatorias vigentes reales en la base de datos."""
    profile = state.get("profile", {})
    query = state.get("query", "")
    rubro = profile.get("rubro")

    # Si en la consulta vino un código de convocatoria puntual:
    code_match = re.search(r"\b(\d{4,8})\b", query)
    specific_code = code_match.group(1) if code_match else None

    found_tenders = []
    try:
        factory = tenders._session_factory()
        with factory() as session:
            q = (
                select(db.Tender)
                .where(db.Tender.state == "vigente")
                .options(selectinload(db.Tender.documents), selectinload(db.Tender.chunks))
                .order_by(db.Tender.published_at.desc())
            )
            if specific_code:
                q = q.where(db.Tender.code == specific_code)
            elif rubro:
                q = q.where(db.Tender.rubros.like(f"%{rubro}%"))

            rows = session.scalars(q).all()

            # Si no encontró con rubro específico, traer las vigentes más recientes para evaluar
            if not rows and not specific_code:
                q_all = (
                    select(db.Tender)
                    .where(db.Tender.state == "vigente")
                    .options(selectinload(db.Tender.documents), selectinload(db.Tender.chunks))
                    .order_by(db.Tender.published_at.desc())
                    .limit(5)
                )
                rows = session.scalars(q_all).all()

            for t in rows:
                # Traer requisitos de chunks si existen
                chunks_data = [
                    {"page": c.page, "text": c.text, "source": c.source}
                    for c in t.chunks
                ]
                found_tenders.append({
                    "id": t.id,
                    "code": t.code,
                    "title": t.title or t.description,
                    "entity": t.entity,
                    "department": t.department,
                    "method": t.method,
                    "rubros": t.rubros,
                    "estimated_value": t.estimated_value,
                    "published_at": t.published_at,
                    "deadline_at": t.deadline_at,
                    "documents": [{"title": d.title, "url": d.url, "is_bases": d.is_bases} for d in t.documents],
                    "chunks": chunks_data,
                })
    except tenders.TendersUnavailable as e:
        return {"tenders_found": [], "status": "db_unavailable", "db_reason": str(e)}
    except Exception:
        return {"tenders_found": [], "status": "db_unavailable", "db_reason": "sin_conexion"}

    if not found_tenders:
        return {"tenders_found": [], "status": "no_tenders"}

    return {"tenders_found": found_tenders, "status": "tenders_found"}


def evaluate_tenders_node(state: AssistantState) -> Dict[str, Any]:
    """Evalúa cada convocatoria contra el perfil del proveedor y determina el semáforo y citas."""
    if state.get("status") == "db_unavailable":
        return {"evaluations": []}

    profile = state.get("profile", {})
    tenders_list = state.get("tenders_found", [])
    evaluations = []

    from services import eligibility

    user_exp = profile.get("experience_amount", None)
    has_rnp = profile.get("has_rnp", None)
    has_consortium = profile.get("has_consortium", False)
    consortium_exp = profile.get("consortium_experience", False)
    has_osinergmin = profile.get("has_osinergmin", None)

    for t in tenders_list:
        chunks = t.get("chunks", [])
        code = t.get("code")
        title = t.get("title", "")
        method = t.get("method", "")
        value = t.get("estimated_value")

        # Buscar requisitos específicos en los fragmentos de las bases
        page_ref = None
        quote_text = "No hay requisitos de las bases disponibles para comprobar."
        req_exp_amount = None
        experience_required = False
        req_osinergmin = False
        req_rnp = False

        for c in chunks:
            c_text = c["text"]
            if re.search(r"\brnp\b|registro nacional de proveedores", c_text, re.I):
                req_rnp = True
            if "osinergmin" in c_text.lower() or "registro de hidrocarburos" in c_text.lower():
                req_osinergmin = True
                page_ref = c.get("page")
                quote_text = c_text[:250].strip()
            if "facturaci" in c_text.lower() or "experiencia" in c_text.lower():
                experience_required = True
                page_ref = c.get("page")
                quote_text = c_text[:250].strip()
                minimum = eligibility._minimum(c_text, "experiencia|facturacion", value)
                if minimum is not None:
                    req_exp_amount = minimum

        # Sin bases o sin umbral interpretable no se inventan montos ni páginas.
        incomplete = not chunks or (experience_required and req_exp_amount is None)

        # Lógica de Semáforo
        # 1. RNP: Si no tiene RNP y va solo -> ROJO (el RNP es requisito indispensable de la Ley de Contrataciones)
        # 2. Habilitación (OSINERGMIN): si la base lo pide y el usuario no lo tiene -> ROJO (salvo consorcio con socio habilitado)
        # 3. Experiencia: Si nunca vendió o tiene S/ 0 de experiencia:
        #    - Si la base pide experiencia alta y no tiene consorcio -> ROJO
        #    - Si puede consorciarse con socio con experiencia -> AMARILLO
        #    - Si cumple habilitación y experiencia (o monto menor) -> VERDE

        semaphore = "amarillo" if incomplete else "verde"
        reasons = (["Falta revisar las bases o interpretar el requisito de experiencia; no se asume un umbral fijo ni un porcentaje del valor."]
                   if incomplete else [])

        if req_rnp and has_rnp is False:
            semaphore = "rojo"
            reasons.append("Declara no contar con el RNP exigido en las bases; el consorcio no sustituye la inscripción propia.")
        elif req_rnp and has_rnp is None:
            if semaphore != "rojo":
                semaphore = "amarillo"
            reasons.append("Falta confirmar el RNP exigido en las bases.")

        if req_osinergmin:
            if has_osinergmin is False and not (has_consortium and profile.get("consortium_habilitation")):
                semaphore = "rojo"
                reasons.append(f"Se exige Registro de Hidrocarburos ante OSINERGMIN (Bases, pág. {page_ref}), con el cual indicas no contar.")
            elif has_osinergmin is True or (has_consortium and profile.get("consortium_habilitation")):
                reasons.append(f"Cumples con la habilitación legal de OSINERGMIN exigida en las Bases (pág. {page_ref}).")
            else:
                if semaphore != "rojo":
                    semaphore = "amarillo"
                reasons.append("Falta confirmar la habilitación de OSINERGMIN que mencionan las bases.")

        # Evaluación de experiencia
        effective_exp = user_exp if user_exp is not None else 0.0
        if req_exp_amount is not None and req_exp_amount > 0:
            if user_exp is None:
                if semaphore != "rojo":
                    semaphore = "amarillo"
                reasons.append("Falta declarar la experiencia facturada para contrastarla con el mínimo de las bases.")
            elif effective_exp >= req_exp_amount:
                reasons.append(f"Tu facturación acreditada (S/ {effective_exp:,.2f}) supera el requisito mínimo de S/ {req_exp_amount:,.2f} (Bases, pág. {page_ref}).")
            else:
                # Falta experiencia
                if has_consortium or consortium_exp:
                    semaphore = "amarillo" if semaphore != "rojo" else "rojo"
                    reasons.append(
                        f"Tu facturación individual (S/ {effective_exp:,.2f}) no cubre los S/ {req_exp_amount:,.2f} requeridos (Bases, pág. {page_ref}), "
                        f"pero **es viable postulando en CONSORCIO** si tu socio aporta la experiencia en la especialidad."
                    )
                else:
                    semaphore = "rojo"
                    reasons.append(
                        f"Exige acreditar facturación acumulada de S/ {req_exp_amount:,.2f} (Bases, pág. {page_ref}). "
                        f"Al no contar con ventas previas suficientes ni consorcio, tu propuesta sería descalificada."
                    )

        if not reasons:
            reasons.append("Cumple preliminarmente con los requisitos de calificación del objeto contractual.")

        evaluations.append({
            "code": code,
            "title": title,
            "entity": t.get("entity"),
            "department": t.get("department"),
            "method": method,
            "estimated_value": value,
            "semaphore": semaphore,
            "page_ref": page_ref,
            "quote": quote_text,
            "reasons": reasons,
            "documents": t.get("documents", []),
        })

    return {"evaluations": evaluations}


def generate_response_node(state: AssistantState) -> Dict[str, Any]:
    """Redacta una respuesta profesional, estructurada con semáforos, citas y recomendaciones."""
    evaluations = state.get("evaluations", [])
    profile = state.get("profile", {})
    status = state.get("status")

    if status == "db_unavailable":
        guide = preparation_guide_node({})["response"]
        return {"response": "No se pudo consultar la base de convocatorias. Todavía no es posible emitir un veredicto para un proceso concreto.\n\n" + guide}

    if not evaluations:
        resp = (
            "No encontré convocatorias vigentes en la base de datos que coincidan exactamente con tu criterio. "
            "Actualmente tenemos registradas convocatorias de los rubros piloto de **Combustibles y lubricantes (1510)** "
            "y **Vehículos de motor (2510)**. Si tu rubro es distinto, puedes solicitar su ingesta desde el catálogo de rubros."
        )
        return {"response": resp}

    lines = []
    lines.append("### 🎯 Convocatorias Vigentes Evaluadas para tu Perfil\n")
    lines.append(
        "He evaluado las convocatorias activas en el SEACE contrastando tu perfil, requisitos habilitantes, "
        "experiencia y las bases administrativas:\n"
    )

    for e in evaluations:
        sem = e["semaphore"]
        if sem == "verde":
            icon = "🟢 **POSTULACIÓN RECOMENDADA (CUMPLE REQUISITOS)**"
        elif sem == "amarillo":
            icon = "🟡 **POSTULACIÓN VIABLE EN CONSORCIO / CON CONDICIONES**"
        else:
            icon = "🔴 **NO RECOMENDADO (RIESGO DE DESCALIFICACIÓN)**"

        lines.append(f"#### {icon}")
        lines.append(f"**Proceso {e['code']}**: {e['title']}")
        lines.append(f"- **Entidad:** {e['entity']} ({e['department']})")
        lines.append(f"- **Procedimiento:** {e['method']}")
        if e['estimated_value']:
            lines.append(f"- **Valor referencial:** S/ {e['estimated_value']:,.2f}")
        source = f"Bases, pág. {e['page_ref']}" if e.get("page_ref") else "Bases, sin página identificada"
        lines.append(f"- **Requisito en Bases:** *«{e['quote']}»* (**{source}**)")
        lines.append(f"- **Diagnóstico:** {' '.join(e['reasons'])}\n")

    # Resumen y recomendación para empresarios nuevos
    lines.append("---\n")
    lines.append("💡 **Recomendación para proveedores nuevos:**")
    if not profile.get("sold_to_state"):
        lines.append(
            "- Si nunca has contratado con el Estado, tu mejor estrategia para licitaciones con exigencia de facturación "
            "es **asociarte en Consorcio** con una empresa que aporte la experiencia requerida, mientras tú aportas el bien o la capacidad de entrega."
        )
    if profile.get("has_rnp") is False:
        lines.append(
            "- Recuerda tramitar tu inscripción en el **RNP (Registro Nacional de Proveedores)** ante el OSCE/OECE; "
            "es un trámite 100% virtual y obligatorio antes de enviar cualquier propuesta."
        )

    response_text = "\n".join(lines)
    return {"response": response_text}


# ---------------------------------------------------------------------------
# Construcción del Grafo
# ---------------------------------------------------------------------------
def build_assistant_graph():
    graph = StateGraph(AssistantState)
    graph.add_node("start", start_node)
    graph.add_node("check_profile", check_profile_node)
    graph.add_node("request_data", request_data_node)
    graph.add_node("preparation_guide", preparation_guide_node)
    graph.add_node("search_tenders", search_tenders_node)
    graph.add_node("evaluate_tenders", evaluate_tenders_node)
    graph.add_node("generate_response", generate_response_node)

    graph.add_edge(START, "start")
    graph.add_edge("start", "check_profile")
    graph.add_conditional_edges(
        "check_profile",
        route_after_profile_check,
        {
            "request_data": "request_data",
            "preparation_guide": "preparation_guide",
            "search_tenders": "search_tenders",
        }
    )
    graph.add_edge("request_data", END)
    graph.add_edge("preparation_guide", END)
    graph.add_edge("search_tenders", "evaluate_tenders")
    graph.add_edge("evaluate_tenders", "generate_response")
    graph.add_edge("generate_response", END)

    return graph.compile(name="Assistant: Tender Qualification")


ASSISTANT_GRAPH = build_assistant_graph()


def ask_assistant(query: str, history: Optional[List[Dict[str, str]]] = None, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compatibilidad del grafo original; la web autenticada usa eligibility_chat.answer (HU-010)."""
    history = history or []
    profile = profile or {}

    state_input = {
        "query": query,
        "messages": history,
        "profile": profile,
    }

    result = ASSISTANT_GRAPH.invoke(state_input)
    return {
        "response": result.get("response", ""),
        "profile": result.get("profile", profile),
        "evaluations": result.get("evaluations", []),
        "status": result.get("status", "answered"),
    }
