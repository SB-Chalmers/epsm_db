"""Observed building systems, read once from an EPC's raw source fields.

These are interpretations, not model choices: what the certificate says about
ventilation, heating carriers and the heat-pump type. Conflicts stay visible and
the source values are kept, so a consumer decides what to do with them. Carrier
families are those of the measured heating target, so a model built from these
systems and the target it is compared with agree by construction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .epc_heating import _number, certificate_raw, heating_fields, regime_of

VENTILATION_FIELDS = {
    "VentTypSjalvdrag": "S",
    "VentTypF": "F",
    "VentTypFmed": "Fmed",
    "VentTypFT": "FT",
    "VentTypFTX": "FTX",
}
# The stem of each EgiPump field, with or without the regime's UPPV suffix.
HEAT_PUMP_TYPES = {
    "Mark": "ground",
    "Franluft": "exhaust_air",
    "LuftLuft": "air_air",
    "LuftVatten": "air_water",
}
_YES = {"ja", "true", "1"}


@dataclass(frozen=True)
class ObservedSystems:
    """What one EPC reports about a building's systems.

    ``ventilation`` is S, F, Fmed (exhaust with heat recovery), FT or FTX, and
    None unless exactly one system is reported; ``ventilation_status`` says why.
    F and Fmed together count as Fmed. ``reported_ventilation`` keeps every system
    the certificate marks, including conflicts. ``heating_carriers`` lists the
    families with positive energy. ``heat_pump_type`` is the type with the most
    electricity, and None without a heat pump.
    """

    certificate_id: str
    ventilation: str | None
    ventilation_status: str
    reported_ventilation: tuple[str, ...]
    heating_carriers: tuple[str, ...]
    heat_pump_type: str | None
    raw_values: dict

    @property
    def heating_carrier(self):
        """The carrier when exactly one is reported, as the heating target requires."""
        return self.heating_carriers[0] if len(self.heating_carriers) == 1 else None

    def as_dict(self):
        return asdict(self)


def _marked(raw, field):
    values = raw.get(field, [])
    return len(values) == 1 and str(values[0]).strip().casefold() in _YES


def _ventilation(raw):
    reported = tuple(kind for field, kind in VENTILATION_FIELDS.items() if _marked(raw, field))
    systems = {"Fmed"} if set(reported) == {"F", "Fmed"} else set(reported)
    if len(systems) == 1:
        return next(iter(systems)), "reported", reported
    return None, "conflicting" if systems else "unavailable", reported


def _heat_pump_type(raw, members):
    totals = {}
    for field in members:
        stem = field.removeprefix("EgiPump").removesuffix("UPPV")
        amount = _number(raw, field)
        if stem in HEAT_PUMP_TYPES and amount:
            totals[HEAT_PUMP_TYPES[stem]] = amount
    return max(totals, key=totals.get) if totals else None


def observed_systems_from_raw(certificate_id, raw):
    """Read the systems one EPC's raw source fields report."""
    fields = heating_fields(regime_of(certificate_id))
    carriers = tuple(
        family
        for family, members in fields.items()
        if sum(_number(raw, field) or 0 for field in members) > 0
    )
    ventilation, status, reported = _ventilation(raw)
    return ObservedSystems(
        certificate_id,
        ventilation,
        status,
        reported,
        carriers,
        _heat_pump_type(raw, fields.get("heat_pump", ())),
        {key: list(value) for key, value in raw.items()},
    )


def observed_systems_from_record(record):
    """Read the systems of a record's uniquely selected EPC."""
    certificate_id, heating, _ = certificate_raw(record, "Egi")
    _, ventilation, _ = certificate_raw(record, "VentTyp")
    return observed_systems_from_raw(certificate_id, heating | ventilation)
