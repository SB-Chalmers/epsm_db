"""Deterministic EPC identity decisions; quantities and physical history stay separate."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import re
import unicodedata

from .linkage import derive_epc_candidates, apply_global_cardinalities

POLICY_VERSION = "epc-identity-address-scope-v2"


def text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).upper().split())


def address_key(municipality, address, postcode):
    """Preserve explicit letters and punctuation; normalize spacing and case only."""
    match = re.fullmatch(r"(.+?)\s+(\d+)\s*([A-ZÅÄÖ]?)", text(address))
    postal = re.sub(r"\s", "", str(postcode or ""))
    if not match or not re.fullmatch(r"\d{5}", postal):
        return None
    street, number, suffix = match.groups()
    return str(municipality), street, number, suffix, postal


def lm_address(row):
    values = row.get("original_values", {})
    prefix = "Belagenhetsadress/Adressplatsattribut/"

    def one(path):
        v = values.get(path, [])
        return v[0] if isinstance(v, list) and len(v) == 1 else ""

    street = one("Belagenhetsadress/Adressomrade/faststalltNamn") or row.get("street_name")
    number = one(prefix + "Adressplatsbeteckning/adressplatsnummer") or row.get("address_number")
    suffix = one(prefix + "Adressplatsbeteckning/bokstavstillagg")
    postal = one(prefix + "postnummer") or row.get("postcode")
    return address_key(
        row.get("municipality_code"), f"{street or ''} {number or ''}{suffix}", postal
    )


def subject_rows(subject):
    return subject.get("source_rows") or [subject.get("original_values", {})]


def subject_addresses(subject):
    return {
        key
        for p in subject_rows(subject)
        if (key := address_key(subject.get("municipality_code"), p.get("IdAdr"), p.get("IdPostnr")))
    }


def assess_epc(
    buildings,
    properties,
    designations,
    subjects,
    addresses=(),
    *,
    events=(),
    assessment_date=None,
    shared_address_houses=None,
    address_building_counts=None,
    exact_address_keys=None,
    strict_accepted_subjects=None,
):
    """Same rules for all functions. Absence of history never establishes continuity."""
    buildings, properties, designations, subjects = map(
        list, (buildings, properties, designations, subjects)
    )
    as_of = date.fromisoformat(str(assessment_date or date.today()))
    by_building = {str(b["building_id"]): b for b in buildings}
    relations = derive_epc_candidates(buildings, properties, designations, subjects)
    by_subject = defaultdict(list)
    for r in relations:
        r["match_method"] = "exact_property_house"
        r["reason_codes"] = (
            (r["rejection_reason"] or "").split("|") if r["rejection_reason"] else []
        )
        by_subject[r["epc_subject_id"]].append(r)
    prop_by_building = defaultdict(list)
    for p in properties:
        prop_by_building[str(p["building_id"])].append(p)
    prop_keys = defaultdict(set)
    from .linkage import property_match_key

    for p in designations:
        key = p.get("match_key") or property_match_key(
            p.get("municipality_code"), p.get("designation_normalized")
        )
        if key:
            prop_keys[str(p["property_unit_id"])].add(key)
    exact, base = defaultdict(list), defaultdict(list)
    for a in addresses:
        key = lm_address(a)
        if key:
            a = dict(a, building_id=str(a["building_id"]), address_key=list(key))
            exact[key].append(a)
            base[key[:3] + key[4:]].append(a)
    shared = defaultdict(set)
    if shared_address_houses:
        for key, houses in shared_address_houses.items():
            shared[key].update(houses)
    cert_subjects = defaultdict(set)
    for s in subjects:
        cert = s.get("certificate_id", str(s["formular_id"]))
        cert_subjects[cert].add(s["epc_subject_id"])
        for key in subject_addresses(s):
            shared[(s["property_match_key"], key)].add(s.get("house_number"))
    results = []
    for s in sorted(subjects, key=lambda s: s["epc_subject_id"]):
        sid = s["epc_subject_id"]
        cert = s.get("certificate_id", str(s["formular_id"]))
        rows = subject_rows(s)
        keys = subject_addresses(s)
        warnings = []
        if any(str(p.get("IdRapportLM") or "").strip() for p in rows):
            warnings.append("source_identity_warning")
        if any(len(shared[(s["property_match_key"], k)]) > 1 for k in keys):
            warnings.append("shared_address_distinct_houses")
        if len(keys) != 1 or any(
            address_key(s.get("municipality_code"), p.get("IdAdr"), p.get("IdPostnr")) is None
            for p in rows
        ):
            warnings.append("missing_or_conflicting_address")
        approval = s.get("certificate_date")
        approval = date.fromisoformat(str(approval)) if approval else None
        matched_events = [
            e
            for e in events
            if e["municipality_code"] == s.get("municipality_code")
            and (
                e.get("property_match_key") == s["property_match_key"]
                or address_key(e["municipality_code"], e.get("address"), e.get("postcode")) in keys
            )
            and date.fromisoformat(e["reported_date"]) <= as_of
            and (approval is None or date.fromisoformat(e["reported_date"]) > approval)
        ]
        if matched_events:
            warnings.append("reported_site_change")
        if approval and approval > as_of:
            warnings.append("certificate_after_assessment_date")
        existing = by_subject[sid]
        temporal = "site_change_warning" if matched_events else "unknown"

        def physical_conflict(b):
            year = b.get("construction_year")
            return bool(approval and year and int(year) > approval.year)

        for r in existing:
            b = by_building[r["building_id"]]
            r["evidence"].update(
                epc_house_number=s.get("house_number"),
                register_house_number=b.get("house_number"),
                register_construction_year=b.get("construction_year"),
                certificate_date=str(approval) if approval else None,
            )
            if physical_conflict(b) and r["decision"] == "accepted":
                r["decision"] = "rejected"
                r["reason_codes"].append("construction_after_certificate")
                r["rejection_reason"] = "|".join(r["reason_codes"])
                r["evidence_tier"] = None
        if (
            not any(r["decision"] == "accepted" for r in existing)
            and len(keys) == 1
            and sid not in (strict_accepted_subjects or ())
        ):
            key = next(iter(keys))
            hits = exact.get(key, [])
            method = "same_property_exact_address"
            if not hits and not key[3] and key not in (exact_address_keys or ()):
                hits = base.get(key[:3] + key[4:], [])
                method = "same_property_missing_address_suffix"
            hit_ids = {a["building_id"] for a in hits}
            hit_count = (address_building_counts or {}).get(key, len(hit_ids))
            fallback_block = list(warnings)
            if hit_count != 1 and hit_count:
                fallback_block.append("address_multiple_buildings")
            for bid in sorted(hit_ids):
                b = by_building[bid]
                eligible = [p for p in prop_by_building[bid] if p["overlap_fraction"] >= 0.5]
                matching = [
                    p
                    for p in prop_by_building[bid]
                    if s["property_match_key"] in prop_keys[str(p["property_unit_id"])]
                ]
                if not matching:
                    matching = [p for p in prop_by_building[bid] if p.get("is_primary_property")]
                for p in matching:
                    unit = str(p["property_unit_id"])
                    blocked = list(fallback_block)
                    if s["property_match_key"] not in prop_keys[unit]:
                        blocked.append("property_identity_conflict")
                    if not b.get("linkage_eligible"):
                        blocked.append("unresolved_footprint")
                    if len(eligible) != 1:
                        blocked.append("ambiguous_property_assignment")
                    if p["overlap_fraction"] < 0.5:
                        blocked.append("insufficient_spatial_support")
                    if b.get("municipality_code") != s.get("municipality_code"):
                        blocked.append("municipality_conflict")
                    if b.get("is_existing") is not True:
                        blocked.append("existing_status_unverified")
                    if physical_conflict(b):
                        blocked.append("construction_after_certificate")
                    evidence = dict(
                        policy_version=POLICY_VERSION,
                        minimum_overlap_fraction=0.5,
                        epc_house_number=s.get("house_number"),
                        register_house_number=b.get("house_number"),
                        epc_property_key=s["property_match_key"],
                        current_property_keys=sorted(prop_keys[unit]),
                        address_key=list(key),
                        address_building_count=hit_count,
                        address_records=[a for a in hits if a["building_id"] == bid],
                        register_construction_year=b.get("construction_year"),
                        certificate_date=str(approval) if approval else None,
                    )
                    codes = list(blocked)
                    if s.get("house_number") != b.get("house_number"):
                        codes.append("house_number_conflict")
                    if method.endswith("suffix"):
                        codes.append("address_suffix_omitted")
                    prior = next(
                        (
                            r
                            for r in existing
                            if r["building_id"] == bid and r["property_unit_id"] == unit
                        ),
                        None,
                    )
                    if prior is None:
                        prior = dict(
                            building_id=bid,
                            epc_subject_id=sid,
                            formular_id=s["formular_id"],
                            certificate_id=cert,
                            property_unit_id=unit,
                            overlap_fraction=float(p["overlap_fraction"]),
                        )
                        relations.append(prior)
                        existing.append(prior)
                    else:
                        evidence["strict_rejection_reasons"] = prior["reason_codes"]
                    prior.update(
                        decision="rejected" if blocked else "accepted",
                        match_method=method,
                        rejection_reason="|".join(sorted(set(blocked))) or None,
                        reason_codes=sorted(set(codes)),
                        evidence_tier=None if blocked else "C_guarded_address",
                        evidence=evidence,
                    )
        accepted = [r for r in existing if r["decision"] == "accepted"]
        codes = set(warnings)
        if accepted:
            codes.update(c for r in accepted for c in r["reason_codes"])
        else:
            codes.update(c for r in existing for c in r["reason_codes"])
            codes.add("no_accepted_candidate" if existing else "no_candidate")
        if "construction_after_certificate" in codes and not accepted:
            temporal = "construction_conflict"
        elif "certificate_after_assessment_date" in warnings:
            temporal = "certificate_after_assessment_date"
        results.append(
            dict(
                subject_id=sid,
                outcome="matched"
                if accepted
                else "unresolved_candidates"
                if existing
                else "no_candidate",
                match_methods=sorted({r["match_method"] for r in accepted}),
                reporting_scope="declared_multiple_subjects"
                if len(cert_subjects[cert]) > 1
                else "declared_single_subject",
                temporal_status=temporal,
                assessment_date=as_of,
                reasons=sorted(codes),
                details=dict(
                    accepted_building_ids=sorted({r["building_id"] for r in accepted}),
                    candidate_building_ids=sorted({r["building_id"] for r in existing}),
                    declared_subject_count=len(cert_subjects[cert]),
                    source_locators=s.get("source_locators", []),
                    source_attributes={
                        field: sorted(
                            {str(p.get(field)) for p in rows if p.get(field) not in (None, "")}
                        )
                        for field in (
                            "IdAdr",
                            "IdPostnr",
                            "IdRapportLM",
                            "IdHusnr",
                            "IdFastBet",
                            "EgenTypkod",
                            "EgenTypkod_typ",
                            "EgenNybyggAr",
                            "EgenAntalPlan",
                            "EgenAtemp",
                            "EgenAtempSkolor",
                            "EgenAtempBad",
                        )
                    },
                    events=matched_events,
                    physical_continuity="not_established",
                    policy_version=POLICY_VERSION,
                ),
            )
        )
    sources_by_id = {s["epc_subject_id"]: s for s in subjects}
    for r in relations:
        b = by_building[r["building_id"]]
        r["evidence"].update(
            source_locators=sources_by_id[r["epc_subject_id"]].get("source_locators", []),
            register_run_id=b.get("register_run_id"),
            footprint_area_m2=b.get("footprint_area_m2"),
            component_count=b.get("component_count"),
            building_purposes=b.get("purposes", []),
        )
        r["evidence"].update(
            policy_version=POLICY_VERSION,
            match_method=r["match_method"],
            overlap_fraction=r["overlap_fraction"],
            reason_codes=r["reason_codes"],
        )
    return apply_global_cardinalities(relations), results
