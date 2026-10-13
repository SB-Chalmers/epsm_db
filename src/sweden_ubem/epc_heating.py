"""Auditable EPC delivered-heating targets and measurement-period comparisons.

The source-form values are preserved. A reported period target and an inferred
normal-year target have different weather bases and must never be interchanged.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date


POST_HEATING = {
    "district_heat": ("EgiFjarrvarmeUPPV",),
    "electric_direct": ("EgiElVattenUPPV", "EgiElDirektUPPV", "EgiElLuftUPPV"),
    "heat_pump": ("EgiPumpMarkUPPV", "EgiPumpFranluftUPPV", "EgiPumpLuftLuftUPPV", "EgiPumpLuftVattenUPPV"),
    "fuel": ("EgiOljaUPPV", "EgiGasUPPV", "EgiVedUPPV", "EgiFlisUPPV", "EgiOvrBiobransleUPPV"),
}
PRE_HEATING = {family: tuple(field.removesuffix("UPPV") for field in fields) for family, fields in POST_HEATING.items()}
POST_DHW = ("EgiFjarrvarmeVV", "EgiOljaVV", "EgiGasVV", "EgiVedVV", "EgiFlisVV", "EgiOvrBiobransleVV", "EgiElVV")
POST_OTHER = POST_DHW + ("EgiFjarrkyla", "EgiKomfort", "EgiFastighet")


def _unique(raw, field):
    values = raw.get(field, [])
    if not isinstance(values, list):
        values = [values]
    values = list(dict.fromkeys(str(value).strip() for value in values if value is not None))
    return values[0] if len(values) == 1 and values[0] else None


def _number(raw, field):
    value = _unique(raw, field)
    if value is None:
        return None
    try:
        result = float(value.replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _has_observation(raw, field):
    values = raw.get(field, [])
    if not isinstance(values, list):
        values = [values]
    return any(value is not None and str(value).strip() for value in values)


def _month(raw, field):
    value = _unique(raw, field)
    if value is None or not value.isdigit() or len(value) not in (4, 6):
        return None
    year = int(value[:-2]) + (2000 if len(value) == 4 else 0)
    month = int(value[-2:])
    return date(year, month, 1) if 1900 <= year <= 2100 and 1 <= month <= 12 else None


def _next_month(value):
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


@dataclass(frozen=True)
class EPCHeatingTarget:
    certificate_id: str
    regime: str
    carrier: str | None
    period_start: date | None
    period_end_exclusive: date | None
    reported_period_heating_kwh: float | None
    inferred_normal_year_heating_kwh: float | None
    normal_year_inference: str | None
    raw_values: dict
    mapping_statuses: dict
    exclusions: tuple[str, ...]

    @property
    def years(self):
        if self.period_start is None or self.period_end_exclusive is None:
            return ()
        return tuple(range(self.period_start.year, self.period_end_exclusive.year + (self.period_end_exclusive.month > 1)))

    def as_dict(self):
        return asdict(self)


def heating_target_from_raw(certificate_id, raw, *, mapping_statuses=None):
    """Build two explicitly labelled targets from one EPC's raw source fields.

    Missing carrier fields are retained as missing. A single positive carrier
    family is a candidate, not proof that absent alternative carriers are zero.
    """
    regime = "after_v260" if certificate_id.startswith("efter_v260:") else "before_v260" if certificate_id.startswith("fore_v260:") else "unknown"
    start, last = _month(raw, "EgiForstaArManad"), _month(raw, "EgiSistaArManad")
    end = _next_month(last) if last else None
    exclusions = []
    if start is None or end is None or (end.year - start.year) * 12 + end.month - start.month != 12:
        exclusions.append("measurement_period_not_twelve_months")
    if (_unique(raw, "EgiBeraknatVarde") or "").casefold() != "nej":
        exclusions.append("not_measurement_indicated")
    fields = POST_HEATING if regime == "after_v260" else PRE_HEATING if regime == "before_v260" else {}
    observed = {field: _number(raw, field) for members in fields.values() for field in members}
    invalid = [field for field in observed if _has_observation(raw, field) and observed[field] is None]
    if invalid:
        exclusions.append("invalid_heating_fields")
    positive = {family: sum(observed[field] or 0 for field in members) for family, members in fields.items()}
    positive = {family: amount for family, amount in positive.items() if amount > 0}
    carrier = next(iter(positive)) if len(positive) == 1 else None
    if carrier is None:
        exclusions.append("mixed_or_missing_carrier")
    total = next(iter(positive.values())) if carrier else None
    reported = None
    normal = None
    inference = None
    if regime == "after_v260":
        if not exclusions:
            reported = total
        # Preserve the normalized target independently of whether a measured
        # period or comparable single carrier is available.
        components = tuple(observed) + POST_OTHER
        values = [_number(raw, field) for field in components]
        source_total = _number(raw, "EgiSumma2")
        normalized_total = _number(raw, "EgiEnergianvandning")
        invalid_other = any(_has_observation(raw, field) and _number(raw, field) is None for field in POST_OTHER)
        exact = source_total is not None and source_total > 0 and abs(sum(v or 0 for v in values) - source_total) <= 1e-6
        if exact and not invalid and not invalid_other and normalized_total is not None and normalized_total > 0 and not any((_number(raw, field) or 0) > 0 for field in ("EgiFjarrkyla", "EgiKomfort")):
            other = sum(_number(raw, field) or 0 for field in POST_DHW + ("EgiFastighet",))
            candidate = normalized_total - other
            if candidate > 0:
                normal = candidate
                inference = "normal_year_total_minus_reconciled_dhw_and_property"
    elif not exclusions:
        if regime == "before_v260":
            dhw = _number(raw, "EgiVVBered")
            if dhw is not None and total > dhw:
                reported = total - dhw
            else:
                exclusions.append("combined_heating_dhw_unresolved")
    return EPCHeatingTarget(certificate_id, regime, carrier, start, end, reported, normal, inference, dict(raw), dict(mapping_statuses or {}), tuple(exclusions))


def heating_target_from_record(record):
    certificate = record.selected_certificate
    if certificate is None:
        raise ValueError("A uniquely selected EPC is required")
    certificate_id = certificate["certificate_id"]
    observations = [row for row in record.observations if row["certificate_id"] == certificate_id and row["source_field"].startswith("Egi")]
    raw = {}
    statuses = {}
    for row in observations:
        field = row["source_field"]
        raw.setdefault(field, []).extend(row["raw_values"])
        statuses.setdefault(field, []).append(row.get("mapping_status"))
    return heating_target_from_raw(certificate_id, raw, mapping_statuses=statuses)


def period_heating_kwh(hourly, year, start, end):
    """Sum hourly carrier input over [start, end), on a local-standard-time year.

    Full-year AMYs are simulated separately; this slices only months belonging
    to the EPC's twelve-month interval, including periods crossing years.
    """
    import numpy as np
    from epsm_core.weather import hourly_axis

    values = np.asarray(hourly, dtype=float)
    axis = hourly_axis(year)
    if len(values) != len(axis) or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Hourly carrier input must be a finite nonnegative complete year")
    return float(values[(axis >= np.datetime64(start)) & (axis < np.datetime64(end))].sum())
