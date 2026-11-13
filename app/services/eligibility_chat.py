"""Respuesta del asistente autenticado a partir del mismo motor de HU-010 que usa la ficha."""
import re

from sqlalchemy import select

from services import accounts, assistant, db, eligibility, eligibility_store, tenders
from services.formatting import format_pen_total


def sidebar(profile, previous=None):
    result = assistant.extract_profile_updates(" ".join(str(profile.get(k) or "") for k in ("business_activity", "rubros")), previous or {})
    if profile.get("similar_contracts_amount") is not None:
        result["experience_amount"] = float(profile["similar_contracts_amount"])
    result["company_name"] = profile.get("company_name", "")
    result["ruc"] = profile.get("ruc", "")
    return result


def _code(query):
    explicit = re.search(r"(?:convocatoria|proceso|licitacion|licitación|codigo|código)\s*(?:n[°º.]?\s*)?[:#-]?\s*(\d{4,10})\b", query, re.I)
    if explicit:
        return explicit.group(1)
    if re.fullmatch(r"\d{4,10}", query.strip()):
        return query.strip()
    reference = re.search(r"\b(?:a la|al|para la|para el)\s+(\d{4,10})\b", query, re.I)
    if reference:
        return reference.group(1)
    return None


def answer(query, account_factory, user_id, history=None, profile=None):
    company = accounts.get_profile(account_factory, user_id)
    context = sidebar(company, profile)
    updates = assistant.extract_profile_updates(query, context)
    context = sidebar(company, updates)
    if assistant.check_profile_node({"query": query, "profile": updates}).get("status") == "general_guidance":
        return {"response": assistant.preparation_guide_node({})["response"], "profile": context, "evaluations": []}
    declared = assistant.extract_profile_updates(query, {})
    if "experience_amount" in declared and company.get("similar_contracts_amount") is not None:
        if eligibility.decimal(declared["experience_amount"]) != eligibility.decimal(company["similar_contracts_amount"]):
            return {"response": "La experiencia de tu consulta difiere de la guardada en **Mi empresa**. Actualiza allí el monto facturado en contratos similares, o corrige la consulta, antes de emitir el veredicto. No se sustituyen silenciosamente los datos del perfil.", "profile": context, "evaluations": []}
    code = _code(query) or context.get("selected_tender_code")
    if code:
        codes = [code]
        # Un RNP declarado explícitamente en el chat se conserva en los datos de esa evaluación.
        declarations = assistant.extract_profile_updates(query, {})
        if "has_rnp" in declarations:
            values = eligibility_store.load_inputs(account_factory, user_id, codes).get(code, {})
            values["rnp"] = "yes" if declarations["has_rnp"] else "no"
            eligibility_store.save_inputs(account_factory, user_id, code, values)
        context["selected_tender_code"] = code
    else:
        rubro = updates.get("rubro")
        declared = re.findall(r"\b\d{4}\b", str(company.get("rubros") or ""))
        rubro = rubro or (declared[0] if declared else None)
        if not rubro:
            return {"response": "Indique el **código de la convocatoria** o **qué bienes o productos vende tu empresa**. Puede completar el giro y los rubros en **Mi empresa** para conservarlos.", "profile": context, "evaluations": []}
        with tenders._session_factory()() as session:
            codes = session.scalars(select(db.Tender.code).where(db.Tender.state == "vigente",
                db.Tender.rubros.like(f"%{rubro}%")).order_by(db.Tender.published_at.desc()).limit(3)).all()
    if not codes:
        return {"response": "No hay convocatorias vigentes cargadas para el rubro consultado. Revise el catálogo de rubros o indique un código concreto.", "profile": updates, "evaluations": []}
    results = list(eligibility.evaluate_many(account_factory, user_id, codes).values())
    if not results:
        return {"response": "No encontré ese código en las convocatorias guardadas. Compruebe el código en Convocatorias vigentes.", "profile": context, "evaluations": []}
    lines = ["### Elegibilidad de su empresa",
             f"**Empresa:** {company.get('company_name') or 'Por completar'} · **RUC:** {company.get('ruc') or 'Pendiente en Mi empresa'}.",
             "Resultado preliminar según el perfil guardado, las declaraciones y los documentos de su cuenta.\n"]
    for result in results:
        color = result["color"].capitalize()
        lines += [f"#### {color} · Convocatoria {result['code']}", f"**{result['label']}**", result["title"] or "", ""]
        if result["amount"] is not None:
            lines.append(f"- **Monto:** {format_pen_total(result['amount'])} ({result['amount_source']}).")
        else:
            lines.append("- **Monto:** falta el monto; no se inventa un valor.")
        if result["guarantee"]["amount"] is not None:
            lines.append(f"- **Garantía de fiel cumplimiento orientativa:** {format_pen_total(result['guarantee']['amount'])}.")
            lines.append("- **Sustento del cálculo:** Reglamento de la Ley 32069 (DS 009-2025-EF y modificatorias): art. 113, garantías; art. 114, retención; art. 139, excepciones. Confirme su aplicación en las bases.")
        lines.append(f"- **Documentos evaluados al {result['on_date']}:** {result['date_basis']}.")
        for check in result["checks"]:
            location = f" (bases, página {check['page']})" if check.get("page") else " (bases, sin página identificada)" if check.get("text") else ""
            lines.append(f"- **{check['label']}{location}:** {check['reason']}")
            if check.get("text"):
                lines.append("> " + check["text"][:350].replace("\n", " ") + ("…" if len(check["text"]) > 350 else ""))
        lines.append("Complete o revise las declaraciones en la ficha de esta convocatoria para resolver los puntos amarillos.\n")
    lines.append("El tipo y la vigencia declarada de un PDF no verifican su autenticidad. Antes de postular, contraste el resultado con las bases oficiales.")
    return {"response": "\n".join(lines), "profile": context, "evaluations": results}
