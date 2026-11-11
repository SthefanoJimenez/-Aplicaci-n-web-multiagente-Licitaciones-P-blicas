"""Garantías orientativas para bienes bajo Ley 32069; parámetros versionados y configurables.

Fuentes revisadas el 11/11/2026: DS 009-2025-EF arts. 113, 114 y 139,
modificado por DS 001-2026-EF; UIT 2026, DS 301-2025-EF.
Una MYPE puede optar por retención: no se reduce el porcentaje por ser MYPE.
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


def calculate(amount, values, on_date=None, policy=None):
    """No decide solvencia a partir de ventas ni aplica normativa a un régimen desconocido."""
    policy = policy or Policy.configured()
    day = on_date or date.today()
    result = {"status": "pending", "amount": None, "accessory": None, "advance": None,
              "rate": policy.rate * 100, "source": SOURCE, "exemption_source": EXEMPTION_SOURCE,
              "uit_source": UIT_SOURCE, "retention_allowed": False, "exempt": False}
    if values.get("regime") != "32069" or day.year != policy.year or day < policy.effective_from:
        return result | {"reason": "Confirme en las bases el régimen y los parámetros de garantía aplicables a este año."}
    if amount is None:
        return result | {"reason": "Falta el monto para calcular la garantía."}
    amount = Decimal(str(amount))
    if not values.get("whole_contract"):
        return result | {"reason": "Confirme que el monto comprende todos los ítems o contratos que se le adjudicarían en este proceso."}
    exempt = amount <= policy.exempt_uit * policy.uit
    principal = Decimal("0") if exempt else (amount * policy.rate).quantize(Decimal("0.01"))
    accessory_base = Decimal(str(values.get("accessory_amount") or "0"))
    advance_base = Decimal(str(values.get("advance_amount") or "0"))
    accessory = Decimal("0") if exempt else (accessory_base * policy.accessory_rate).quantize(Decimal("0.01"))
    advance = (advance_base * policy.advance_rate).quantize(Decimal("0.01"))
    retention = bool(values.get("mype") or amount <= policy.retention_limit)
    result |= {"amount": principal, "accessory": accessory, "advance": advance,
               "retention_allowed": retention, "exempt": exempt}
    note = "Cálculo orientativo sobre el monto evaluado; debe recalcularse con el monto contractual. "
    if exempt:
        note += "El monto no supera 50 UIT: excepción del art. 139, sujeta a la suma de ítems adjudicados al mismo postor. "
    if values.get("guarantee_method") == "retention" and retention:
        needed = advance  # Los adelantos no se garantizan mediante retención.
        note += "Retención disponible según art. 114; ser MYPE no elimina ni reduce la garantía. "
    else:
        needed = principal + accessory + advance
        if values.get("guarantee_method") == "retention":
            return result | {"reason": note + "La retención no resulta aplicable con los datos declarados; revise otra modalidad."}
    if needed > 0 and not values.get("guarantee_method"):
        return result | {"reason": note + "Falta confirmar la modalidad prevista de garantía."}
    capacity = values.get("guarantee_capacity")
    if needed > 0 and (capacity is None or Decimal(str(capacity)) < needed):
        return result | {"reason": note + "Falta acreditar cobertura suficiente de la garantía; las ventas no prueban esa capacidad."}
    if values.get("accessory_required") and not values.get("accessory_amount"):
        return result | {"reason": note + "Falta el monto de las prestaciones accesorias exigidas en las bases."}
    return result | {"status": "pass", "reason": note + "Cobertura declarada suficiente para el cálculo mostrado."}
