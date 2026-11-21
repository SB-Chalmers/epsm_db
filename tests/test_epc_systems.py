from types import SimpleNamespace

import pytest

from epsm_db.epc_heating import heating_target_from_raw
from epsm_db.epc_systems import observed_systems_from_raw, observed_systems_from_record


@pytest.mark.parametrize(
    "marked, ventilation, status",
    [
        (("VentTypF",), "F", "reported"),
        (("VentTypSjalvdrag",), "S", "reported"),
        (("VentTypFmed",), "Fmed", "reported"),
        (("VentTypF", "VentTypFmed"), "Fmed", "reported"),
        (("VentTypFT", "VentTypFTX"), None, "conflicting"),
        ((), None, "unavailable"),
    ],
)
def test_ventilation_keeps_conflicts_and_their_source(marked, ventilation, status):
    raw = {field: ["Ja"] for field in marked} | {"VentTypS": ["Nej"]}
    systems = observed_systems_from_raw("efter_v260:1", raw)
    assert (systems.ventilation, systems.ventilation_status) == (ventilation, status)
    assert len(systems.reported_ventilation) == len(marked)


def test_a_repeated_ventilation_value_is_not_read_as_marked():
    assert observed_systems_from_raw("efter_v260:1", {"VentTypF": ["Ja", "Nej"]}).ventilation is None


def test_carriers_use_the_measured_targets_families_and_regime():
    raw = {"EgiFjarrvarmeUPPV": ["100"], "EgiFjarrvarme": ["900"], "EgiElDirekt": ["50"]}
    after = observed_systems_from_raw("efter_v260:1", raw)
    assert after.heating_carriers == ("district_heat",)
    assert after.heating_carrier == heating_target_from_raw("efter_v260:1", raw).carrier
    before = observed_systems_from_raw("fore_v260:1", raw)
    assert before.heating_carriers == ("district_heat", "electric_direct")
    assert before.heating_carrier is None
    assert observed_systems_from_raw("unknown:1", raw).heating_carriers == ()


def test_heat_pump_type_is_the_one_with_the_most_electricity():
    raw = {"EgiPumpFranluftUPPV": ["3000"], "EgiPumpMarkUPPV": ["1000"], "EgiPumpLuftLuftUPPV": ["x"]}
    systems = observed_systems_from_raw("efter_v260:1", raw)
    assert systems.heat_pump_type == "exhaust_air"
    assert systems.heating_carrier == "heat_pump"
    assert observed_systems_from_raw("efter_v260:1", {"EgiOljaUPPV": ["1"]}).heat_pump_type is None


def test_record_reads_only_the_selected_certificate():
    def row(certificate, field, value):
        return {"certificate_id": certificate, "source_field": field, "raw_values": [value]}

    record = SimpleNamespace(
        selected_certificate={"certificate_id": "efter_v260:2"},
        observations=[
            row("efter_v260:1", "VentTypFTX", "Ja"),
            row("efter_v260:2", "VentTypF", "Ja"),
            row("efter_v260:2", "EgiPumpMarkUPPV", "2000"),
            row("efter_v260:2", "Atemp", "120"),
        ],
    )
    systems = observed_systems_from_record(record)
    assert (systems.ventilation, systems.heat_pump_type) == ("F", "ground")
    assert "Atemp" not in systems.raw_values
    with pytest.raises(ValueError, match="uniquely selected"):
        observed_systems_from_record(SimpleNamespace(selected_certificate=None, observations=[]))
