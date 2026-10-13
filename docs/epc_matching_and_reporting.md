# EPC matching and transparent reporting

An accepted link identifies a current building candidate. It does **not** establish
that an older certificate measures the building as it exists today. Quantities stay
with their documented certificate or calculation owner, even when several building
subjects are listed. The rules below apply to every building function, not only
schools. No manual-review queue is required.

## How an identity link is established

1. **Exact property and house.** Match the EPC municipality and normalized property
   designation, with an exact register house number. The building needs resolved
   geometry and exactly one property covering at least 50% of its footprint.
   Multiple building UUIDs passing this rule remain explicit many-to-many links.
2. **Guarded exact address.** Only if no exact link remains, compare full street,
   number, supplied letter and postcode. All current matches must identify one
   building UUID in the same municipality, on the EPC-stated property. The same
   spatial checks apply and the register must verify an existing building.
3. **Guarded missing letter.** If the EPC supplies no letter and has no exact
   address match, enumerate letter variants of that number and postcode. All
   variants must identify one UUID; apply the checks in step 2. Never remove a
   letter that the EPC explicitly supplied.

Address fallback is withheld when different EPC house references share the address
on that property, the source reports an identity/address problem, the subject's raw
rows contain missing/conflicting addresses, there are competing current UUIDs, or
published site-change evidence applies. A current property discrepancy produces a
candidate with an explicit reason, not an inferred historical property crosswalk.
Area/floor similarity and the largest footprint never decide identity.

A candidate that would otherwise be accepted is rejected if its register
construction year is later than the certificate approval year. An update timestamp
alone never triggers this check. Year-level data cannot resolve changes within a
year. Approval date is not a measurement-period or construction-completion date.

These are project acceptance rules, not official Boverket or LM guarantees. Guarded
address links remain a separately labelled evidence tier; they do not prove physical
continuity. A school-specific whitelist or a guessed historical UUID would not be
suitable for a national pipeline, so neither is used.

## What is recorded

| Record | Meaning |
|---|---|
| `evidence.epc_subject` | Original parsed certificate/property/house reference. Repeated raw rows remain intact. |
| `evidence.epc_candidate` | Every evaluated building/subject/property pair; accepted/rejected decision, method, overlap, reasons and compared attributes. |
| `evidence.epc_subject_result` | One outcome per parsed subject, including zero-candidate subjects; source locators, source attributes, reporting scope and temporal warning. |
| `evidence.epc_site_event` | Optional published site evidence; date basis, explanation, URL and pinned extraction/document hashes. It does not identify a demolished building UUID. |
| `catalog.process_run` | Input source versions, assessment date, policy, field-mapping digest and code/schema digest. |

Subject outcomes are `matched`, `unresolved_candidates` and `no_candidate`.
The reporting scope is `declared_single_subject` or `declared_multiple_subjects`;
this describes the loaded source references, not a verified physical allocation.
Invalid source identities remain raw rows with an `invalid_subject` finding;
they are counted separately from successfully parsed subjects.

Temporal status is separate from matching:

- `unknown`: physical continuity has not been established. This is the default,
  including accepted exact and guarded address links.
- `site_change_warning`: a dated publication reports a physical change at the
  site after the EPC approval and by the assessment date. The affected individual
  UUID and actual event date may remain unknown.
- `construction_conflict`: an otherwise matching candidate has a construction
  year after the EPC approval year and is not automatically accepted.
- `certificate_after_assessment_date`: the EPC approval follows that date.

The assessment date controls these diagnostics. It does not reconstruct an older
building stock snapshot or produce a simulation-ready energy dataset. A model
preparation policy must separately select dated measurements, resolve their owner
and reporting scope, and decide how to handle warnings/unknown continuity.

## Optional published change evidence

The initialization bundle `site_event_files` entry accepts `epc-site-event-v1` JSON. Each event supplies municipality,
property key or complete address/postcode, kind, reported date, date basis,
description, source URL and `evidence_scope: "site"`. A `source_document` path is
relative to the JSON and must have a matching `source_document_sha256`. Referenced
PDFs and the JSON are registered and hashed with the run. URL-only evidence is
allowed, but pins the extracted assertion rather than the remote document bytes.

The supplied Uddevalla file contains three previously investigated municipal
publications: Herrestad demolition reporting, Ramneröd reconstruction and the new
Äsperöd preschool. Their PDFs are under `output/sources/epc-site-events/`. Reporting
period ends and publication dates are explicitly distinguished from actual event
dates. Matching the old property label is a site-level association, not proof of
historical property identity. No publication scraping or human classification is
required to complete ingestion: without this optional source, historical continuity
remains unknown. National history should come from structured, dated source feeds
when available; published examples are supplemental evidence.

```sh
.venv/bin/sweden-ubem dataset process --dataset uddevalla --municipality 1485 \
  --assessment-date 2026-10-12
.venv/bin/sweden-ubem dataset report-epc --dataset uddevalla \
  --output output/validation/my-epc-report
```

## Reporting and releases

The report writes JSON and CSV tables, a summary, plain-language explanations and a
file-hash manifest. It starts from **all loaded EPC subjects**, independently of
accepted building links. It reports matched/unmatched subjects and fully/partly/
completely unmatched certificates, plus reason and temporal-status counts. A
certificate appearing in multiple reason categories must not be summed across
categories. Property tax codes describe property classification; they do not
necessarily describe the measured use. For example, a school-classified property
can contain a certificate reporting swimming/sports use.

Read-only users can query `mart.epc_subject_results`, `mart.epc_candidates`,
`mart.epc_certificates`, `mart.epc_site_events`, `mart.epc_diagnostic_summary` and
`mart.process_runs`. `UBEM.epc_results()` includes unmatched subjects;
`UBEM.epc_summary()` supplies grouped counts. Building selection exports include
all subjects/results of their linked certificates. Use `report-epc` for the whole
loaded cohort, including completely unmatched certificates.

Each selection is an immutable publication over shared national source evidence and a pinned processing release. Complete certificates are assessed once per processing release; all candidate buildings of selected certificates remain inspectable as support, and their other certificates remain excluded. Loaded layers are not replaced;
create a new dataset ID to recompute. EPC candidates, results and site events are
append-only. Current views select one validated active release per municipality,
so adding another municipality does not replace the current Uddevalla release.
Activation requires successful validation and loaded EPC linkage. Old releases
remain queryable by dataset ID. The development rebuild is a clean schema rebuild
with a database backup, not an in-place compatibility migration.

Compare releases using certificate/subject/building identities. If source versions
are identical but policy/code digests differ, an outcome change is an algorithm
change. A new source version establishes changed source evidence, not necessarily a
physical change. Source evidence and decision changes are reported separately.

## Uddevalla result, 12 October 2026

The rebuilt stock contains 45,300 building UUIDs and 4,063 parsed EPC subjects.
All subjects were assessed: 3,972 matched, 85 had unresolved candidate pairs and
6 had no candidate pair. The 3,922 certificates comprise 3,835 fully matched,
4 partly matched and 83 completely unmatched certificates.

On identical rebuilt inputs, the exact-house baseline matched 3,974 subjects.
The general guarded rules recovered four subjects: two small-house properties,
one sports property and the missing-letter Östrabo school case. Six previously
matched subjects were withheld because current construction years follow their
EPC approval dates. These are source-date conflicts, not confirmed replacement
events. The school-classified group improved from 91 to 92 matches out of 100.
The actual candidate attributes, figures and full unmatched list are under
`output/validation/uddevalla-2026-10-12/`.
