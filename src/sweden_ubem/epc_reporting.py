"""Read-only EPC diagnostics including subjects that have no building link."""

from __future__ import annotations

import csv
from datetime import date
import json
from pathlib import Path

from .database import _connect
from .hashing import content_hash, sha256_file
from .epc_assessment import POLICY_VERSION

METHODS = {
    "exact_property_house": "the exact property and house-number rule",
    "same_property_exact_address": "the guarded full-address rule on the same property",
    "same_property_missing_address_suffix": "the guarded missing-address-letter rule on the same property",
}

REASONS = {
    "no_candidate": "No building/property candidate pair was available from the loaded property and address evidence.",
    "no_accepted_candidate": "Candidates were found, but none passed the automatic identity rules.",
    "house_number_disagreement": "The EPC and register house numbers differ or one is missing.",
    "house_number_conflict": "The address rule identified a building whose register house number differs from the EPC.",
    "address_suffix_omitted": "The EPC omits an address letter. Current letter variants were checked; their UUID count is recorded in the candidate evidence.",
    "source_identity_warning": "The EPC source reports an identity/address problem; address fallback is withheld.",
    "shared_address_distinct_houses": "Different EPC house references share this address; an address alone cannot distinguish them.",
    "missing_or_conflicting_address": "A complete, consistent street number and postcode is unavailable across the subject's source rows.",
    "address_multiple_buildings": "The address or its allowed letter variants refer to multiple building UUIDs.",
    "property_identity_conflict": "The address candidate is on a different current property; historical property continuity is not established.",
    "construction_after_certificate": "The candidate's recorded construction year follows the EPC approval year; automatic identity linking is withheld.",
    "reported_site_change": "Published evidence reports physical change at this site; it does not identify which individual building UUID changed.",
    "certificate_after_assessment_date": "The certificate approval follows the requested assessment date.",
    "ambiguous_property_assignment": "The building does not have exactly one property covering at least half its footprint.",
    "insufficient_spatial_support": "The EPC-stated property covers less than half the candidate footprint.",
    "unresolved_footprint": "The candidate has no usable linkage geometry.",
    "municipality_conflict": "The current building and EPC municipality identities do not agree.",
    "existing_status_unverified": "The current register does not verify the address candidate as an existing building.",
}


def read_site_events(path, *, source_root=None):
    document = json.loads(Path(path).read_text())
    if document.get("contract") != "epc-site-event-v1":
        raise ValueError("Unsupported EPC site event contract")
    required = {
        "municipality_code",
        "event_kind",
        "reported_date",
        "date_basis",
        "description",
        "source_url",
        "evidence_scope",
    }
    allowed = required | {
        "event_id",
        "property_match_key",
        "address",
        "postcode",
        "source_document",
        "source_document_sha256",
    }
    result = []
    for item in document.get("events", []):
        if not required <= item.keys() or item.keys() - allowed:
            raise ValueError("Incomplete or unknown site event fields")
        if item["evidence_scope"] != "site" or not (
            item.get("property_match_key") or item.get("address") and item.get("postcode")
        ):
            raise ValueError(
                "Site event needs a property or complete address; UUID continuity is not supported"
            )
        if item["date_basis"] not in ("publication_date", "opening_date", "report_period_end"):
            raise ValueError("Unknown site event date basis")
        date.fromisoformat(item["reported_date"])
        if item["event_kind"] not in ("demolition", "replacement", "reconstruction"):
            raise ValueError("Unsupported physical event kind")
        if not item["source_url"].startswith("https://"):
            raise ValueError("A source URL is required")
        if bool(item.get("source_document")) != bool(item.get("source_document_sha256")):
            raise ValueError("Source document and checksum must be supplied together")
        if item.get("source_document"):
            from .bundle import translated_reference
            translated_reference(path, item["source_document"], source_root=source_root,
                                 expected_sha256=item["source_document_sha256"])
        projected = {k: v for k, v in item.items() if k != "source_document"}
        result.append(dict(projected, event_id=item.get("event_id") or content_hash(item)))
    if len({r["event_id"] for r in result}) != len(result):
        raise ValueError("Duplicate site event IDs")
    return result


def explain(row):
    if row["outcome"] == "matched":
        lead = (
            "An automatic building identity link was established using "
            + ", ".join(METHODS.get(method, method) for method in row["match_methods"])
            + "."
        )
    elif row["outcome"] == "no_candidate":
        lead = "No building/property candidate pair was available for automatic linking."
    else:
        lead = "No automatic building identity link was established."
    return " ".join(
        [
            lead,
            *[REASONS.get(code, code) for code in row["reasons"]],
            "Physical continuity at the assessment date is not established.",
        ]
    )


def epc_report(url, dataset, output):
    """Stream complete EPC diagnostics; retain only compact aggregate counters."""
    from collections import Counter
    import shutil
    from uuid import uuid4
    from psycopg import sql

    output = Path(output)
    if output.exists():
        raise FileExistsError("Choose a new EPC report path")
    stage = output.with_name(output.name + ".staging-" + uuid4().hex)
    stage.mkdir(parents=True)
    counts = Counter()
    cert_counts = Counter()
    reasons = Counter()
    temporal = Counter()
    functions = {}
    table_counts = {}
    accepted = invalid = 0
    unmatched = stage / "unmatched.part"
    try:
        with unmatched.open("w", encoding="utf-8") as markdown, _connect(url) as c:
            raw_count = c.execute(
                "SELECT count(*) FROM mart.epc_raw_inventory WHERE dataset_id=%s", (dataset,)
            ).fetchone()[0]
            for name in (
                "epc_subject_results",
                "epc_subjects",
                "epc_candidates",
                "epc_certificates",
                "epc_site_events",
                "process_runs",
                "source_inventory",
                "quality_findings",
            ):
                count = 0
                with c.cursor(name="report_" + uuid4().hex) as cursor:
                    cursor.itersize = 4096
                    cursor.execute(
                        sql.SQL("SELECT to_jsonb(v) FROM mart.{} v WHERE dataset_id=%s").format(
                            sql.Identifier(name)
                        ),
                        (dataset,),
                    )
                    with (stage / f"{name}.json").open("w", encoding="utf-8") as stream:
                        stream.write("[\n")
                        csv_stream = writer = None
                        try:
                            for (row,) in cursor:
                                if name == "epc_subject_results":
                                    counts[row["outcome"]] += 1
                                    reasons.update(set(row["reasons"]))
                                    temporal[row["temporal_status"]] += 1
                                    codes = (
                                        row["details"]
                                        .get("source_attributes", {})
                                        .get("EgenTypkod", [])
                                    )
                                    code = (
                                        codes[0]
                                        if len(codes) == 1
                                        else "conflicting"
                                        if codes
                                        else "unknown"
                                    )
                                    function = functions.setdefault(
                                        code, {"total": 0, "matched": 0, "unmatched": 0}
                                    )
                                    function["total"] += 1
                                    function[
                                        "matched" if row["outcome"] == "matched" else "unmatched"
                                    ] += 1
                                    row["explanation"] = explain(row)
                                    if row["outcome"] != "matched":
                                        markdown.write(
                                            f"- **{row['certificate_id']} — {row['designation_normalized']}, house {row['house_number']}**: {row['explanation']}\n"
                                        )
                                elif name == "epc_certificates":
                                    cert_counts[row["matching_status"]] += 1
                                elif name == "epc_candidates":
                                    accepted += row["decision"] == "accepted"
                                elif name == "quality_findings":
                                    invalid += row["code"] == "invalid_subject"
                                if count:
                                    stream.write(",\n")
                                stream.write(json.dumps(row, ensure_ascii=False, default=str))
                                if writer is None:
                                    csv_stream = (stage / f"{name}.csv").open(
                                        "w", newline="", encoding="utf-8"
                                    )
                                    writer = csv.DictWriter(csv_stream, fieldnames=list(row))
                                    writer.writeheader()
                                writer.writerow(
                                    {
                                        k: json.dumps(v, ensure_ascii=False, default=str)
                                        if isinstance(v, (dict, list))
                                        else v
                                        for k, v in row.items()
                                    }
                                )
                                count += 1
                        finally:
                            if csv_stream:
                                csv_stream.close()
                        stream.write("\n]\n")
                table_counts[name] = count
        summary = dict(
            dataset=dataset,
            policy_version=POLICY_VERSION,
            parsed_subjects=table_counts["epc_subjects"],
            assessed_subjects=table_counts["epc_subject_results"],
            subjects={
                name: counts[name] for name in ("matched", "unresolved_candidates", "no_candidate")
            },
            certificates={
                name: cert_counts[name]
                for name in (
                    "fully_matched",
                    "partly_matched",
                    "unmatched",
                    "not_assessed",
                    "no_parsed_subjects",
                )
            },
            raw_rows=raw_count,
            accepted_relations=accepted,
            reasons=dict(sorted(reasons.items())),
            temporal_status=dict(sorted(temporal.items())),
            property_tax_codes=functions,
            invalid_subject_rows=invalid,
            quantities_allocated=False,
            physical_continuity_established=False,
            count_scope="loaded complete certificates; property tax codes are not EPC measured-use functions",
        )
        (stage / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        with (
            (stage / "report.md").open("w", encoding="utf-8") as stream,
            unmatched.open(encoding="utf-8") as source,
        ):
            stream.write(
                f"# EPC assessment: {dataset}\n\nPolicy: `{POLICY_VERSION}`. Counts cover the loaded certificate cohort.\n\n"
            )
            stream.write(
                f"{summary['assessed_subjects']:,} subjects assessed: {counts['matched']:,} matched; {counts['unresolved_candidates']:,} with unresolved candidates; {counts['no_candidate']:,} with no candidate.\n\n"
            )
            stream.write(
                "Identity matching does not verify physical continuity. Quantities remain at their documented owner and are never allocated to footprints.\n\n## Unmatched subjects\n\n"
            )
            shutil.copyfileobj(source, stream)
        unmatched.unlink()
        manifest = dict(
            summary=summary, files={p.name: sha256_file(p) for p in sorted(stage.iterdir())}
        )
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        stage.rename(output)
        return dict(output=str(output), **summary)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
