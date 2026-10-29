"""Render the saved, read-only Uddevalla EPC evaluation as a PDF.

Uses reportlab; run with the bundled PDF Python runtime when needed.
"""

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image,
    Table,
    TableStyle,
    PageBreak,
)


def render(root, output):
    root = Path(root)
    evaluation = json.loads((root / "evaluation/evaluation.json").read_text())
    summary = json.loads((root / "epc/summary.json").read_text())
    maps = json.loads((root / "evaluation/maps.json").read_text())
    candidates = json.loads((root / "epc/epc_candidates.json").read_text())
    certificates = {
        c["certificate_id"]: c for c in json.loads((root / "epc/epc_certificates.json").read_text())
    }
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle("BodyPlain", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=9)
    )
    styles.add(
        ParagraphStyle("SmallPlain", fontName="Helvetica", fontSize=8.5, leading=11, spaceAfter=7)
    )
    styles["Title"].fontSize = 23
    styles["Title"].leading = 28
    styles["Title"].textColor = colors.HexColor("#17324d")
    styles["Heading2"].textColor = colors.HexColor("#17324d")
    flow = []

    def paragraph(value, style="BodyPlain"):
        return Paragraph(value, styles[style])

    def add(value, style="BodyPlain"):
        flow.append(paragraph(value, style))

    def image(path, width=480):
        iw, ih = ImageReader(str(path)).getSize()
        flow.append(Image(str(path), width=width, height=width * ih / iw))

    def table(data, widths):
        t = Table(
            [[paragraph(escape(str(x)), "SmallPlain") for x in row] for row in data],
            colWidths=widths,
            hAlign="LEFT",
        )
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0f5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#a9bac8")),
                ]
            )
        )
        flow.append(t)
        flow.append(Spacer(1, 9))

    add("Uddevalla EPC implementation<br/>and evaluation", "Title")
    add("Rebuilt development database | 12 October 2026 | Schema 0.0.2", "SmallPlain")
    add(
        "Every parsed EPC subject now has a recorded outcome, including unmatched subjects. Identity links, declared reporting scope and historical applicability are separate. The same rules apply to all building functions."
    )
    table(
        [
            ["Measure", "Result"],
            ["Building UUIDs", f"{evaluation['counts']['building_facts']:,}"],
            [
                "EPC certificates",
                f"{summary['certificates']['fully_matched'] + summary['certificates']['partly_matched'] + summary['certificates']['unmatched']:,}",
            ],
            [
                "Parsed / assessed subjects",
                f"{summary['parsed_subjects']:,} / {summary['assessed_subjects']:,}",
            ],
            [
                "Matched / unmatched subjects",
                f"{summary['subjects']['matched']:,} / {summary['subjects']['unresolved_candidates'] + summary['subjects']['no_candidate']:,}",
            ],
            [
                "Recovered / withheld versus exact-house baseline",
                f"{len(evaluation['recovered_subjects'])} / {len(evaluation['withheld_subjects'])}",
            ],
            ["Validation", "Passed" if evaluation["validation"]["valid"] else "Failed"],
        ],
        [325, 155],
    )
    image(root / "evaluation/coverage.png")
    add(
        "The baseline and revised rules use identical rebuilt source inputs. These changes therefore measure the matching policy, not changes in the survey or footprint files. The older development database withheld all automatic links; it is not a useful matching-policy baseline.",
        "SmallPlain",
    )
    flow.append(PageBreak())
    add("What the counts mean", "Heading2")
    add(
        "Matched means an automatic identity relation passed the documented rules. It does not prove that an older EPC measures the current physical building. Unmatched subjects remain stored and exported; none are silently removed from the diagnostic cohort."
    )
    table(
        [
            ["Certificate outcome", "Count"],
            *[[k.replace("_", " "), v] for k, v in summary["certificates"].items()],
        ],
        [325, 155],
    )
    table(
        [
            ["Accepted relation method", "Count"],
            *[[k.replace("_", " "), v] for k, v in evaluation["accepted_methods"].items()],
        ],
        [325, 155],
    )
    image(root / "evaluation/reasons.png")
    add(
        "Reason categories overlap. A subject can have both an address conflict and a house-number conflict, so reason counts must not be added to calculate unmatched totals.",
        "SmallPlain",
    )
    flow.append(PageBreak())
    add("Why four links were recovered and six were withheld", "Heading2")
    add(
        "The four recovered subjects pass the guarded address rules: same property and postcode, one current UUID, no shared-house address or source warning, and no recorded construction-date conflict. Two are small-house properties, one is a sports property and one is school-classified. This supports applying the rule to every function."
    )
    table(
        [
            ["Recovered EPC", "Property", "Method"],
            *[
                [
                    s["certificate_id"].split(":", 1)[1],
                    s["designation_normalized"],
                    ", ".join(s["match_methods"]).replace("same_property_", "").replace("_", " "),
                ]
                for s in evaluation["recovered"]
            ],
        ],
        [85, 235, 160],
    )
    add(
        "The six withheld subjects previously passed exact property/house matching. Their current register construction years follow EPC approval. This is a source-date conflict, not proof of demolition or replacement; some differences are only one or two years. The original EPC, candidate identity and compared values remain available. Stronger dated identity/history evidence can change a later automatic assessment."
    )
    date_rows = [
        ["EPC / property", "Approval", "EPC construction year", "Register construction year"]
    ]
    for subject in evaluation["withheld"]:
        affected = [
            r
            for r in candidates
            if r["subject_id"] == subject["subject_id"]
            and "construction_after_certificate" in r["reasons"]
        ]
        years = sorted({str(r["details"]["register_construction_year"]) for r in affected})
        date_rows.append(
            [
                subject["certificate_id"].split(":", 1)[1]
                + " / "
                + subject["designation_normalized"],
                certificates[subject["certificate_id"]]["certificate_date"],
                ", ".join(subject["details"]["source_attributes"].get("EgenNybyggAr", [])),
                ", ".join(years),
            ]
        )
    table(date_rows, [220, 90, 85, 85])
    add(
        "School property classification: 100 subjects, 92 matched and 8 unresolved, compared with 91 matched in the exact-house baseline. The missing-letter Infanterivägen 4 subject is recovered. Shared campus references, conflicting property identities and historical site evidence remain explicit unresolved outcomes. No manual-review queue is created."
    )
    base_recovered = set(evaluation["recovered_subjects"])
    withheld = set(evaluation["withheld_subjects"])
    for item in maps:
        flow.append(PageBreak())
        site = item["property"]
        add(escape(site), "Heading2")
        add(
            "Green footprints have an accepted link for at least one EPC subject on this property. This does not mean that the unresolved subject discussed below matches that building. Grey footprints are other evaluated candidates. Labels show current register house numbers. The teal boundary is the EPC-stated property in the current source, clipped to the candidate area shown. This is a current-stock comparison, not a historical reconstruction.",
            "SmallPlain",
        )
        image(root / "evaluation" / item["file"])
        focus = [
            s
            for s in item["subjects"]
            if s["outcome"] != "matched"
            or s["subject_id"] in base_recovered
            or s["subject_id"] in withheld
        ]
        if not focus:
            add(
                "All loaded subjects for this property pass the exact identity rules. Physical continuity remains separately assessed."
            )
        for s in focus:
            attrs = s["details"].get("source_attributes", {})
            add(
                f"<b>{escape(s['certificate_id'])}, EPC house {s['house_number']}: {escape(s['outcome'].replace('_', ' '))}</b>"
            )
            address = ", ".join(attrs.get("IdAdr", [])) or "not supplied"
            add(
                f"Source address: {escape(address)}. Reported construction year: {escape(', '.join(attrs.get('EgenNybyggAr', [])) or 'unknown')}. Reporting scope: {escape(s['reporting_scope'].replace('_', ' '))}. Temporal status: {escape(s['temporal_status'].replace('_', ' '))}.",
                "SmallPlain",
            )
            relevant = [
                r
                for r in candidates
                if r["subject_id"] == s["subject_id"]
                and (r["decision"] == "accepted" or r["details"].get("address_key"))
            ]
            if relevant:
                add(
                    "Address/accepted candidates: "
                    + escape(
                        "; ".join(
                            f"house {r['details'].get('register_house_number')}, UUID {str(r['building_id'])[:8]}, {r['decision']}, overlap {r['details'].get('overlap_fraction', 0):.1%}"
                            for r in relevant
                        )
                    ),
                    "SmallPlain",
                )
            reasons = ", ".join(s["reasons"]).replace("_", " ")
            add(
                f"Recorded reasons: {escape(reasons or 'exact property/house identity passed')}. No quantity is allocated from this certificate to a footprint.",
                "SmallPlain",
            )
        if site == "NYPONBUSKEN 16":
            add(
                "The investigated sports certificate 1243829 lists houses 3, 7 and 8. Its existing house-3 identity relation remains separate from unresolved houses 7 and 8. A school property tax code does not make its swimming/sports reporting a classroom measurement.",
                "SmallPlain",
            )
    flow.append(PageBreak())
    add("Traceability and operational limits", "Heading2")
    add(
        "The database records the candidate attributes, overlap fractions, source-row locators and file hashes, matching policy, mapping digest and code/schema digest. Outcomes with no candidates are first-class records. Whole-cohort JSON and CSV reports can be queried and exported with the read-only database role."
    )
    add(
        "Dataset releases are immutable. Current views select one validated active release per municipality. New inputs or a new policy require a new dataset ID. The previous development database was backed up before the clean rebuild; compatibility migrations were not retained."
    )
    add(
        "Three municipal reports were downloaded and pinned with checksums. They supply site-level change evidence for Herrestad, Ramneröd and Äsperöd. The evidence does not establish an individual UUID crosswalk. Publication dates and reporting-period ends are not asserted to be demolition dates."
    )
    for event in evaluation["site_events"]:
        add(
            f'<b>{escape(event["property_match_key"])}</b>: {escape(event["event_kind"])}; {event["reported_date"]} ({escape(event["date_basis"])}). {escape(event["description"])} <link href="{escape(event["source_url"])}" color="#2355a3">Municipal source document</link>.',
            "SmallPlain",
        )
    add(
        "Validation covers building-summary grain, height geometry hashes, geometry loading, one assessment per parsed subject and agreement between subject outcomes and accepted links. Unit and disposable PostGIS tests cover the rules, partial certificates, read-only exports, source checksums, immutable releases and backup/restore."
    )
    add(
        "Two early run completion timestamps were transaction-start timestamps and were removed as unavailable. Subsequent completion timestamps use the wall clock. Total measured ingestion time is saved independently in validation.json. This correction does not change EPC decisions.",
        "SmallPlain",
    )
    add("Known limits", "Heading2")
    add(
        "Historical physical continuity remains unknown unless stronger dated source evidence is available. A source update date does not establish physical change. Post-v2.6 Atemp mapping remains unverified where the source documentation is inconsistent. The database records evidence; model preparation must separately resolve quantity ownership, model-year suitability and group allocation."
    )
    add("Reproduce the evaluation", "Heading2")
    add(
        "Run dataset report-epc for the entire loaded cohort, then scripts/evaluate_epc.py against the named dataset. The saved evaluation JSON contains source versions, actual outcomes and the baseline comparison. Figures are generated from the rebuilt database. The supplied EPC policy document explains every rule and diagnostic field.",
        "SmallPlain",
    )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#617185"))
        canvas.drawString(48, 24, "Uddevalla EPC evaluation | 12 October 2026")
        canvas.drawRightString(A4[0] - 48, 24, str(doc.page))
        canvas.restoreState()

    SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=48,
        leftMargin=48,
        topMargin=42,
        bottomMargin=42,
        title="Uddevalla EPC implementation and evaluation",
    ).build(flow, onFirstPage=footer, onLaterPages=footer)
    return output


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(render(a.root, a.output))
