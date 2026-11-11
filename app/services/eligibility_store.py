"""HU-010: declaraciones y conversación privadas, resueltas siempre por el usuario autenticado."""
from sqlalchemy import JSON, ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column

from services import accounts


class EligibilityInputs(accounts.AccountBase):
    __tablename__ = "datos_elegibilidad"
    company_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), primary_key=True)
    tender_code: Mapped[str] = mapped_column(String(80), primary_key=True)
    values: Mapped[dict] = mapped_column(JSON)


class AssistantConversation(accounts.AccountBase):
    __tablename__ = "conversaciones_empresa"
    company_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), primary_key=True)
    messages: Mapped[list] = mapped_column(JSON)
    profile: Mapped[dict] = mapped_column(JSON)


def _company(session, user_id):
    company = session.scalar(select(accounts.Company).where(accounts.Company.user_id == user_id))
    if company is None:
        raise ValueError("No se encontró la empresa de la sesión.")
    return company


def load_inputs(factory, user_id, codes):
    with factory() as session:
        company = _company(session, user_id)
        rows = session.scalars(select(EligibilityInputs).where(
            EligibilityInputs.company_id == company.id, EligibilityInputs.tender_code.in_(codes))).all()
        return {row.tender_code: dict(row.values) for row in rows}


def save_inputs(factory, user_id, code, values):
    with factory.begin() as session:
        company = _company(session, user_id)
        row = session.get(EligibilityInputs, (company.id, str(code)))
        if row is None:
            row = EligibilityInputs(company_id=company.id, tender_code=str(code), values={})
            session.add(row)
        row.values = dict(values)


def load_chat(factory, user_id):
    with factory() as session:
        company = _company(session, user_id)
        row = session.get(AssistantConversation, company.id)
        return (list(row.messages), dict(row.profile)) if row else ([], {})


def save_chat(factory, user_id, messages, profile):
    # El texto completo queda en la base privada, nunca en la cookie ni en trazas externas.
    kept = [{"role": m["role"], "content": str(m["content"])[:20000]} for m in messages[-12:]]
    with factory.begin() as session:
        company = _company(session, user_id)
        row = session.get(AssistantConversation, company.id)
        if row is None:
            row = AssistantConversation(company_id=company.id, messages=[], profile={})
            session.add(row)
        row.messages, row.profile = kept, dict(profile)


def clear_chat(factory, user_id):
    with factory.begin() as session:
        company = _company(session, user_id)
        row = session.get(AssistantConversation, company.id)
        if row:
            session.delete(row)
