from datetime import date

from sweden_ubem.epc_assessment import assess_epc, address_key


def fixtures():
    b = dict(
        building_id="b",
        house_number=42,
        linkage_eligible=True,
        municipality_code="1485",
        is_existing=True,
        construction_year=1963,
    )
    p = dict(building_id="b", property_unit_id="p", overlap_fraction=1.0, is_primary_property=True)
    d = dict(property_unit_id="p", match_key="1485|P 1")
    s = dict(
        epc_subject_id="s",
        certificate_id="efter_v260:1",
        formular_id=1,
        house_number=8,
        municipality_code="1485",
        property_match_key="1485|P 1",
        certificate_date=date(2021, 8, 9),
        original_values=dict(IdAdr="Road 4", IdPostnr="45134", IdRapportLM=""),
    )
    a = [
        dict(
            building_id="b",
            municipality_code="1485",
            street_name="Road",
            address_number=n,
            postcode="45134",
        )
        for n in ("4A", "4B")
    ]
    return [b], [p], [d], [s], a


def test_missing_suffix_all_variants_one_uuid_and_quantity_scope():
    relations, results = assess_epc(*fixtures(), assessment_date="2026-10-12")
    assert len(relations) == 1 and relations[0]["decision"] == "accepted"
    assert relations[0]["match_method"] == "same_property_missing_address_suffix"
    assert results[0]["outcome"] == "matched"
    assert set(results[0]["reasons"]) == {"house_number_conflict", "address_suffix_omitted"}
    assert results[0]["temporal_status"] == "unknown"
    assert "normalized_value" not in relations[0]


def test_explicit_suffix_never_stripped_and_competing_uuids_block():
    b, p, d, s, a = fixtures()
    s[0]["original_values"]["IdAdr"] = "Road 4C"
    _, results = assess_epc(b, p, d, s, a)
    assert results[0]["outcome"] != "matched"
    s[0]["original_values"]["IdAdr"] = "Road 4"
    b.append(dict(b[0], building_id="b2"))
    p.append(dict(p[0], building_id="b2"))
    a[1]["building_id"] = "b2"
    relations, results = assess_epc(b, p, d, s, a)
    assert all(r["decision"] == "rejected" for r in relations)
    assert "address_multiple_buildings" in results[0]["reasons"]


def test_shared_campus_address_and_reported_error_block_all_functions():
    b, p, d, s, a = fixtures()
    a = [dict(a[0], address_number="4")]
    s.append(dict(s[0], epc_subject_id="s2", house_number=9))
    relations, results = assess_epc(b, p, d, s, a)
    assert all(r["decision"] == "rejected" for r in relations)
    assert all("shared_address_distinct_houses" in r["reasons"] for r in results)
    assert all(r["reporting_scope"] == "declared_multiple_subjects" for r in results)
    s = s[:1]
    s[0]["original_values"]["IdRapportLM"] = "Adressuppgifter är fel/saknas"
    _, results = assess_epc(b, p, d, s, a)
    assert results[0]["outcome"] != "matched"
    assert "source_identity_warning" in results[0]["reasons"]


def test_property_conflict_retained_and_no_candidate_has_outcome():
    b, p, d, s, a = fixtures()
    d[0]["match_key"] = "1485|P 2"
    relations, results = assess_epc(b, p, d, s, a)
    assert relations[0]["decision"] == "rejected"
    assert "property_identity_conflict" in results[0]["reasons"]
    _, results = assess_epc(b, p, d, s, [])
    assert results[0]["outcome"] == "no_candidate"


def test_construction_after_certificate_blocks_strict_link_but_update_time_does_not():
    b, p, d, s, a = fixtures()
    b[0].update(house_number=8, construction_year=2025)
    relations, results = assess_epc(b, p, d, s, a)
    assert all(r["decision"] == "rejected" for r in relations)
    assert results[0]["temporal_status"] == "construction_conflict"
    b[0].update(construction_year=1963, revision_timestamp="2026-03-04")
    relations, results = assess_epc(b, p, d, s, a)
    assert relations[0]["decision"] == "accepted"
    assert results[0]["temporal_status"] == "unknown"


def test_site_warning_keeps_exact_identity_but_blocks_fallback_and_is_date_scoped():
    b, p, d, s, a = fixtures()
    events = [
        dict(
            municipality_code="1485",
            property_match_key="1485|P 1",
            reported_date="2025-05-26",
            event_kind="replacement",
        )
    ]
    relations, results = assess_epc(b, p, d, s, a, events=events, assessment_date="2026-10-12")
    assert (
        results[0]["temporal_status"] == "site_change_warning"
        and results[0]["outcome"] != "matched"
    )
    b[0]["house_number"] = 8
    relations, results = assess_epc(b, p, d, s, a, events=events, assessment_date="2026-10-12")
    assert (
        results[0]["outcome"] == "matched"
        and results[0]["temporal_status"] == "site_change_warning"
    )
    _, results = assess_epc(b, p, d, s, a, events=events, assessment_date="2024-01-01")
    assert results[0]["temporal_status"] == "unknown"


def test_conflicting_flattened_source_addresses_and_missing_postcodes_block():
    b, p, d, s, a = fixtures()
    s[0]["source_rows"] = [s[0]["original_values"], dict(IdAdr="Other 9", IdPostnr="45134")]
    _, results = assess_epc(b, p, d, s, a)
    assert results[0]["outcome"] != "matched"
    assert "missing_or_conflicting_address" in results[0]["reasons"]
    assert address_key("1485", "Road 4A", "451 34")[3] == "A"
    assert address_key("1485", "Road 4A", None) is None


def test_regime_qualified_certificates_have_separate_cardinalities():
    b, p, d, s, a = fixtures()
    b[0]["house_number"] = 8
    s.append(dict(s[0], epc_subject_id="s2", certificate_id="fore_v260:1"))
    relations, _ = assess_epc(b, p, d, s, a)
    assert all(r["building_to_certificate_count"] == 2 for r in relations)


def test_bounded_policy_uses_complete_address_ambiguity_context():
    b, p, d, s, a = fixtures()
    key = address_key("1485", "Road 4", "45134")
    relations, _ = assess_epc(b, p, d, s, a, address_building_counts={key: 2})
    assert all(r["decision"] == "rejected" for r in relations)
    assert any("address_multiple_buildings" in r["reason_codes"] for r in relations)
    relations, _ = assess_epc(b, p, d, s, a, strict_accepted_subjects={"s"})
    assert all(r["decision"] == "rejected" for r in relations)
    assert all(r["match_method"] != "same_property_missing_address_suffix" for r in relations)
    relations, _ = assess_epc(b, p, d, s, a, exact_address_keys={key})
    assert all(r["decision"] == "rejected" for r in relations)


def test_site_event_contract_pins_documents_and_rejects_uuid_scope(tmp_path):
    import json
    import pytest
    from sweden_ubem.epc_reporting import read_site_events
    from sweden_ubem.hashing import sha256_file

    doc = tmp_path / "source.pdf"
    doc.write_bytes(b"%PDF-synthetic-document")
    event = dict(
        municipality_code="1485",
        property_match_key="1485|P 1",
        reported_date="2025-05-26",
        date_basis="opening_date",
        event_kind="replacement",
        description="New site building.",
        source_url="https://example.org/report.pdf",
        evidence_scope="site",
        source_document="source.pdf",
        source_document_sha256=sha256_file(doc),
    )
    path = tmp_path / "events.json"
    path.write_text(json.dumps(dict(contract="epc-site-event-v1", events=[event])))
    assert read_site_events(path)[0]["source_document_sha256"] == sha256_file(doc)
    doc.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        read_site_events(path)
    event["evidence_scope"] = "building_uuid"
    path.write_text(json.dumps(dict(contract="epc-site-event-v1", events=[event])))
    with pytest.raises(ValueError, match="Site event"):
        read_site_events(path)
