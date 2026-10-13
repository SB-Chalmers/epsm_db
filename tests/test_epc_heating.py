from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from sweden_ubem.epc_heating import heating_target_from_raw, heating_target_from_record, period_heating_kwh
from epsm_core.weather import hourly_axis


def test_post_v260_keeps_period_and_normal_year_targets_separate():
    raw = {
        "EgiBeraknatVarde": ["Nej"],
        "EgiForstaArManad": ["1907"],
        "EgiSistaArManad": ["2006"],
        "EgiFjarrvarmeUPPV": ["10000"],
        "EgiFjarrvarmeVV": ["2000"],
        "EgiFastighet": ["500"],
        "EgiSumma2": ["12500"],
        "EgiEnergianvandning": ["11500"],
    }
    target = heating_target_from_raw("efter_v260:1", raw)
    assert target.carrier == "district_heat"
    assert target.years == (2019, 2020)
    assert target.period_start == date(2019, 7, 1)
    assert target.period_end_exclusive == date(2020, 7, 1)
    assert target.reported_period_heating_kwh == 10000
    assert target.inferred_normal_year_heating_kwh == 9000
    assert target.normal_year_inference
    raw["EgiBeraknatVarde"] = ["Ja"]
    calculated = heating_target_from_raw("efter_v260:1", raw)
    assert calculated.reported_period_heating_kwh is None
    assert calculated.inferred_normal_year_heating_kwh == 9000


def test_pre_v260_subtracts_reported_dhw_only():
    raw = {"EgiBeraknatVarde": ["Nej"], "EgiForstaArManad": ["201901"],
           "EgiSistaArManad": ["201912"], "EgiFjarrvarme": ["13000"],
           "EgiVVBered": ["3000"]}
    target = heating_target_from_raw("fore_v260:1", raw)
    assert target.reported_period_heating_kwh == 10000
    assert target.inferred_normal_year_heating_kwh is None


def test_missing_dhw_or_mixed_carrier_blocks_pre_v260_target():
    raw = {"EgiBeraknatVarde": ["Nej"], "EgiForstaArManad": ["1901"],
           "EgiSistaArManad": ["1912"], "EgiFjarrvarme": ["13000"]}
    assert heating_target_from_raw("fore_v260:1", raw).reported_period_heating_kwh is None
    raw["EgiVVBered"] = ["3000"]
    raw["EgiElDirekt"] = ["100"]
    assert heating_target_from_raw("fore_v260:1", raw).reported_period_heating_kwh is None


def test_conflicting_source_field_blocks_reported_target():
    raw = {"EgiBeraknatVarde": ["Nej"], "EgiForstaArManad": ["1901"],
           "EgiSistaArManad": ["1912"], "EgiFjarrvarmeUPPV": ["10000"],
           "EgiElDirektUPPV": ["0", "200"]}
    target = heating_target_from_raw("efter_v260:1", raw)
    assert target.reported_period_heating_kwh is None
    assert "invalid_heating_fields" in target.exclusions


def test_record_adapter_preserves_raw_mapping_and_duplicate_observations():
    raw = {"EgiBeraknatVarde": ["Nej"], "EgiForstaArManad": ["1901"],
           "EgiSistaArManad": ["1912"], "EgiFjarrvarmeUPPV": ["10000"]}
    observations = [dict(certificate_id="efter_v260:1", source_field=field,
                         raw_values=values, mapping_status="unverified") for field, values in raw.items()]
    observations.append(dict(certificate_id="efter_v260:1", source_field="EgiFjarrvarmeUPPV",
                             raw_values=["20000"], mapping_status="unverified"))
    record = SimpleNamespace(selected_certificate={"certificate_id": "efter_v260:1"},
                             observations=observations)
    target = heating_target_from_record(record)
    assert target.raw_values["EgiFjarrvarmeUPPV"] == ["10000", "20000"]
    assert target.mapping_statuses["EgiFjarrvarmeUPPV"] == ["unverified", "unverified"]
    assert "invalid_heating_fields" in target.exclusions


def test_period_sums_cross_year_and_leap_calendar():
    for year, expected_hours in ((2019, 4416), (2020, 4368)):
        axis = hourly_axis(year)
        values = np.ones(len(axis))
        assert period_heating_kwh(values, year, date(2019, 7, 1), date(2020, 7, 1)) == expected_hours
    with pytest.raises(ValueError):
        period_heating_kwh([1], 2019, date(2019, 1, 1), date(2020, 1, 1))
