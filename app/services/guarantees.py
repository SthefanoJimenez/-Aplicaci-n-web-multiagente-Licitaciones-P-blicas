"""Garantías orientativas bajo Ley 32069 para bienes, servicios y obras; parámetros versionados y configurables.

Fuentes revisadas el 13/11/2026: DS 009-2025-EF arts. 113, 114 y 139,
modificado por DS 001-2026-EF; UIT 2026, DS 301-2025-EF.
Una MYPE puede optar por retención: no se reduce el porcentaje por ser MYPE.

Obras (revisado el 19/11/2026 en el texto del DS 001-2026-EF publicado en El Peruano el 08/01/2026):
- art. 114.1: la retención procede hasta S/ 480 000 en bienes y servicios y hasta S/ 5 000 000 en obras;
  para una MYPE, con independencia del monto, también en obras.
- art. 139.a: la excepción de 50 UIT es «en los contratos de bienes y servicios»; no alcanza a obras.
  Para la consultoría de obras no se aplica la excepción y se pide confirmarla en las bases.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import os

SOURCE = "https://busquedas.elperuano.pe/dispositivo/NL/2474920-3"
EXEMPTION_SOURCE = "https://www.sat.gob.pe/TransparenciaV3/Portals/0/Docs/NormasOtrasEntidades/DS_009-2025-EF_v2.pdf"
UIT_SOURCE = "https://www.gob.pe/institucion/mef/normas-legales/7540449-301-2025-ef"


@dataclass(frozen=True)
class Policy:
    rate: Decimal = Decimal("0.10")
    accessory_rate: Decimal = Decimal("0.10")
    advance_rate: Decimal = Decimal("1")
    uit: Decimal = Decimal("5500")
    exempt_uit: Decimal = Decimal("50")
    retention_limit: Decimal = Decimal("480000")
    works_retention_limit: Decimal = Decimal("5000000")
    year: int = 2026
    effective_from: date = date(2026, 1, 14)

    @classmethod
    def configured(cls):
        fields = {key: Decimal(os.environ.get("HU010_" + key.upper(), str(value.default)))
                  for key, value in cls.__dataclass_fields__.items() if key not in {"year", "effective_from"}}
        if any(not value.is_finite() or value < 0 for value in fields.values()):
            raise ValueError("Parámetros de garantía inválidos")
        if any(fields[key] > 1 for key in ("rate", "accessory_rate", "advance_rate")):
            raise ValueError("El porcentaje debe estar entre 0 y 1")
        return cls(**fields)


def calculate(amount, values, on_date=None, policy=None, kind="bienes", regime="32069"):
    """Garantías orientativas con lo que se sabe, sin pedir al cliente datos que traen las bases (20/11/2026).

    kind: "bienes", "servicios", "obras" o "consultoria_obras" (services.eligibility.contract_kind).
    regime: "32069" (lo deduce services.eligibility de la fecha y las bases) u otro, que queda por revisar.
    values: mype (de Mi empresa), accessory_required (leído de las bases) y, si la empresa los indicó,
    accessory_amount y advance_amount. No decide solvencia ni capacidad de fianza: muestra cuánto sería.

    status: «pass» si se pudo calcular; «info» si falta el monto (no se publicó: la garantía será el 10 % del
    contrato, no es un incumplimiento); «pending» si el régimen o el año no corresponden a estas reglas.
    """
    policy = policy or Policy.configured()
    day = on_date or date.today()
    result = {"status": "pending", "amount": None, "accessory": None, "advance": None,
              "rate": policy.rate * 100, "source": SOURCE, "exemption_source": EXEMPTION_SOURCE,
              "uit_source": UIT_SOURCE, "retention_allowed": False, "exempt": False,
              "accessory_required": bool(values.get("accessory_required"))}
    if regime != "32069":
        return result | {"reason": "Las bases mencionan otro régimen (Ley 30225); confirme en ellas las garantías aplicables."}
    if day.year != policy.year or day < policy.effective_from:
        return result | {"reason": f"Los parámetros de garantía (UIT y límites) son los de {policy.year}; confírmelos en las bases."}
    percent = f"{policy.rate * 100:.0f} %"
    if amount is None:
        return result | {"status": "info",
                         "reason": f"El monto no está publicado: la garantía de fiel cumplimiento será el {percent} del monto "
                                   "del contrato, mediante carta fianza o póliza de caución"
                                   + (", o retención del pago por ser MYPE." if values.get("mype") else ".")}
    amount = Decimal(str(amount))
    # Art. 139.a: la excepción de 50 UIT es solo para contratos de bienes y servicios.
    exempt = kind in ("bienes", "servicios") and amount <= policy.exempt_uit * policy.uit
    principal = Decimal("0") if exempt else (amount * policy.rate).quantize(Decimal("0.01"))
    accessory_base = values.get("accessory_amount")
    accessory = (Decimal("0") if exempt else (Decimal(str(accessory_base)) * policy.accessory_rate).quantize(Decimal("0.01"))
                 if accessory_base else None)
    advance_base = values.get("advance_amount")
    advance = (Decimal(str(advance_base)) * policy.advance_rate).quantize(Decimal("0.01")) if advance_base else None
    limit = policy.works_retention_limit if kind == "obras" else policy.retention_limit
    retention = bool(values.get("mype") or amount <= limit)
    result |= {"status": "pass", "amount": principal, "accessory": accessory, "advance": advance,
               "retention_allowed": retention, "exempt": exempt}
    if exempt:
        note = ("El monto no supera 50 UIT: no se exige garantía de fiel cumplimiento (art. 139), salvo que se le "
                "adjudiquen otros ítems del mismo proceso y la suma lo supere. ")
    else:
        note = f"Garantía de fiel cumplimiento: {percent} del monto del contrato. "
        if retention:
            note += ("Puede ser carta fianza, póliza de caución o retención del pago"
                     + (" por ser MYPE" if values.get("mype") and amount > limit else "") + " (art. 114). ")
        else:
            note += "Debe ser carta fianza o póliza de caución: el monto supera el límite para la retención del pago (art. 114). "
        if kind in ("obras", "consultoria_obras") and amount <= policy.exempt_uit * policy.uit:
            note += ("La excepción de 50 UIT del art. 139 es para bienes y servicios; no se aplica a "
                     + ("obras. " if kind == "obras" else "la consultoría de obras sin confirmarlo en las bases. "))
    if values.get("mype"):
        note += "Ser MYPE no elimina ni reduce la garantía: permite pedir la retención del pago. "
    if values.get("accessory_required") and not exempt:
        note += f"Las bases piden además garantía por prestaciones accesorias: {percent} de su monto. "
    note += "Si solicita adelantos, se garantizan por el mismo monto. Es un cálculo orientativo sobre el monto evaluado."
    return result | {"reason": note.strip()}