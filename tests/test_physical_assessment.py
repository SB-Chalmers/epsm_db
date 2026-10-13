from sweden_ubem.physical_assessment import ratios, unique_number, summarize


def test_above_ground_and_basement_sensitivity_are_distinct():
    r = ratios(100, 2, 250, 1, 6)
    assert r["gfa_to_atemp"] == 0.8
    assert r["height_per_above_ground_storey_m"] == 3
    assert r["full_footprint_basement_sensitivity_gfa_to_atemp"] == 1.2
    assert r["basement_group"] == "positive"
    assert ratios(100, 2, 200)["basement_group"] == "unknown"
    assert ratios(100, 2, 200, 0)["basement_group"] == "zero"
    assert ratios(100, 0, 250) is None


def test_certificate_quantities_reject_unverified_and_conflicting_values():
    r = {
        "source_field": "EgenAtemp",
        "raw_values": ["100", "200"],
        "owner_kind": "certificate",
        "mapping_status": "verified",
        "value_status": "conflicting",
        "normalized_value": None,
    }
    assert unique_number([r], "EgenAtemp", verified=True) is None
    assert unique_number([r], "EgenAtemp") is None
    r.update(raw_values=["200"], value_status="available", normalized_value={"value": 200})
    assert unique_number([r], "EgenAtemp", verified=True) == 200
    r["owner_kind"] = "unverified"
    assert unique_number([r], "EgenAtemp", verified=True) is None


def test_screening_bands_do_not_force_a_pass():
    report = summarize([ratios(100, 2, 50, height=12), ratios(100, 2, 200, height=6)])
    assert report["gfa_atemp_screening"] == {"severe": 1, "within_0.8_to_1.5": 1}
    assert report["height_screening"] == {"outside_band": 1, "within_2_to_5_m": 1}
