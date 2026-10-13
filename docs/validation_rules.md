# Source definitions and validation rules — v0.0.4

The internal model is a small set of definitions and testable rules, not a graph
database. This document describes required checks; it does **not** claim they are
already implemented or that every delivery mapping has been verified.

For each implemented rule retain: ID/version, authority, document title/version,
section or schema path, saved document checksum, source fields, check, severity
and failure action. Rule versions and code versions are recorded on derived results.

## Documentation review

| Reference | What it establishes | Review status |
|---|---|---|
| [LM building handbook](https://www.lantmateriet.se/globalassets/om-lantmateriet/var-samverkan-med-andra/byggnad-adress-lagenhet/handbok_byggnad.pdf), 3.10, 2026-04-30 | §§5.2–5.4: stable UUID, geometry parts/outline meaning and status. §5.6: purposes. §5.12: presentation-oriented main-building designation. | General definitions checked. This maintenance handbook is not the exact schema for every delivered product. |
| [LM Byggnad download documentation](https://geotorget.lantmateriet.se/geodataprodukter/byggnad-nedladdning-vektor-api?view=dokumentation&page=index.html&version=2025.02) | Product-specific fields, codes, geometry and versions. | Exact version content was not retrievable through the documentation shell in this review. Save/review its schemas and code lists before marking mappings verified. |
| [LM Fastighetsindelning download documentation](https://geotorget.lantmateriet.se/geodataprodukter/fastighetsindelning-nedladdning-vektor-api?view=dokumentation&page=index.html&version=2025.02) | Unit/area-part identity and relationships for the delivered product. | Exact version mapping remains pending; do not substitute a different API/product's schema. |
| Local building/address XML plus accompanying metadata | Delivered namespaces, element paths, repeated records, reference types and coordinate encoding. | Inspecting values is not a substitute for a reviewed product schema. In particular, pin relationship and axis-order definitions. |
| Local Boverket `Datamodell.docx` and mapping workbooks for both EPC formats | Certificate/subject/child keys, field meaning, unit, reporting level and dates. | Field-by-field verification remains required; uncertain mappings stay unverified. |
| [`epc_join` footprint contract](../../epc_join/docs/canonical_footprint_contract.md) and [output definitions](../../epc_join/docs/output_schema.md), backed by `src/epc_linkage/linkage.py` | Project geometry and EPC acceptance conventions. | Current source inspected; pin code/contract versions for the implementation, rather than following a moving file silently. |
| Local height catalogue, geometry register and reconciliation/path-map receipts | Component ID/hash join, measurement status, roof fields and evidence locations. | Local catalogue schema inspected; earlier transfer verification recorded. Source remains unpublished. |
| SCB snapshot request/response metadata and indicator definitions | Area types/codes, periods, units, dimensions and disclosure information. | Uddevalla snapshot acquired on 10 October 2026: 18 RegSO areas (2025), median earned income (2024, both sexes), and the existing population grid. Saved responses and checksums are pinned in the source inventory; database projection is checked separately. |

The handbook does not prescribe our normalized geometry hashes, outline-selection
thresholds or EPC matching policy. LM/Boverket/SCB source definitions and our
processing conventions must remain labelled separately. Documentation `2025.02`
is a proposed pin until the source metadata confirms the corresponding product
version. Never declare an exact delivery verified using the newer handbook alone.

## Required checks

`D` rules interpret source definitions; `P` rules are project processing policies.
Their exact source-field mappings must be reviewed before enforcing them on a load.

| Rule | Check | Authority/reference | Failure behavior |
|---|---|---|---|
| D01 Building identity | Parse the building's own `objektidentitet` as UUID; all normalized children reference that building. | LM handbook §5.2 + delivery schema. | Preserve malformed raw row; no fabricated building/child FK. |
| D02 Geometry ownership | Retain all selected source geometry rows for the UUID, with their source version/FID. Do not assume one polygon. | LM handbook §5.3 + geometry schema. | Flag owner/version conflicts; withhold unjustified aggregate/link. |
| D03 Outline meaning | Preserve source outline labels and code meanings, separately from pipeline roof form. | LM handbook §5.3 + versioned code list. | Keep unknown/unspecified source values; mark unresolved rather than delete. |
| D04 Source versions | Retain object version, timestamp and status; do not merge unrelated source-version cohorts. | Exact LM product schema; handbook §§5.4/5.16 as background. | Record conflict; no latest-by-FID shortcut. |
| D05 Repeated purposes | Parse only schema-recognized purpose elements, keeping each occurrence and nullable main flag. | LM handbook §5.6 + delivered XML schema. | Unknown elements remain raw; cannot become invented purpose categories. |
| D06 Main-building flag | Preserve the supplied flag; do not turn it into EPC quantity allocation. | LM handbook §5.12; EPC scope requires Boverket documentation. | Flag misuse; withhold unsupported allocation. |
| D07 Property identities | Parts and units retain distinct identifiers; preserve designation text and exact registered relationship type. | Exact property/building product schemas, mapping pending. | Keep unresolved raw references; don't infer affiliation from overlap. |
| D08 Entrance/address lookup | Resolve exact UUID references with all entrances preserved; absence is not a missing building. | Building/address XML schemas, mapping pending. | Keep entrance and raw address reference, lookup status/reason. |
| D09 Coordinate handling | Read delivered CRS/axis metadata; verify conversion against control coordinates before indexing. | Exact GML/product specification; local parser convention is supporting evidence only. | Stop spatial derivation on wrong/unknown CRS or axis order. |
| D10 EPC reporting level | An observation has its documented certificate/subject/child owner and regime-specific mapping, never a guessed component owner. | Both Boverket models/workbooks, mapping pending. | Retain raw values; leave uncertain typed meanings unverified. |
| P01 Complete cohort | Select source-version cohort for each UUID before municipality/tile subsetting. Preserve all component lineage. | Reviewed `epc_join` footprint contract and compatible height register. | Stop affected spatial result on incomplete selection. |
| P02 Component identity | Recompute hash using the supplying register's exact 2D normalization/WKB encoder; ID suffix and UUID prefix must agree. | Height geometry contract, not an LM physical-part ID. | Keep raw measurement; withhold mismatched normalized component join. |
| P03 Height association | Require matching component ID **and** input geometry hash within explicit input versions. | Height catalogue/register contract. | No UUID-only fallback; report unmatched/mismatched evidence. |
| P04 Geometry resolution | Automatically union valid polygonal components in the selected UUID cohort; accept overlaps and disconnected parts while preserving component geometry, outline labels and height identities. | User-approved lm-current-cohort-polygon-union-v3 policy; differs from the older epc_join representation gate. | Invalid, empty or nonpolygonal geometry blocks spatial links automatically; representation labels do not require manual review. |
| P05 Property overlap | Union intersections by property unit before calculating area/fraction; fractions remain physically bounded. | Project spatial policy, compared with `epc_join`. | Flag overlapping/duplicate parts or oracle differences; no double-counting. |
| P06 EPC acceptance | Exact property/house first; guarded same-property address or omitted-letter fallback with unique UUID, postcode and current-status checks. Shared house addresses, source warnings and physical-change evidence block fallback; later construction years block otherwise accepted candidates. | epc-identity-address-scope-v2; see [EPC policy](epc_matching_and_reporting.md). Project rule, **not** an official LM acceptance rule. | Store every parsed subject outcome and all candidate decisions with reasons. No property-only broadcast, suffix stripping or manual-review queue. |
| P07 Certificate display | Use verified approval dates, retaining ties and undated records; don't infer date from ID. | Boverket mapping workbooks: ExpertGodkannande, Datum för godkännande; user display policy. | Display date ambiguity/unknown; preserve all accepted certificates. |
| P08 Cardinalities | Distinct certificate/subject/building counts reconcile at their declared local/global scope. | Project relation-closure policy. | Unknown global closure stays unknown, never falsely complete. |
| P09 Area assignments | Keep positive overlaps/ranks, unique primary decision and boundary ties with geometry/area version and run. | Project context policy; SCB supplies areas, not cadastral truth. | Ambiguous primary stays ambiguous; no use in property/EPC matching. |
| P10 Statistical values | Retain all dimensions, period, raw token/unit and transformations; protected population subtotals need not sum. | SCB indicator/response definitions + explicit conversion policy. | Preserve protected/missing values; no inferred occupants. |
| P11 Availability | Distinguish layer not loaded from a loaded lookup with no result; retain field/component coverage counts. | Agreed database policy. | No overall complete-building verdict or hidden layer absence. |
| P12 Row identities | Building/context summary unique by dataset/UUID; quantities unique by their actual owner/observation identity. | Database/query contract. | Fail affected view/export validation; do not deduplicate arbitrary rows to hide a join error. |
| P13 Raw integrity | File hashes unchanged; each selected row resolves to a registered file/member/FID or record locator. | Immutable-source policy. | Stop affected layer on corrupt/checksum/schema failure. |

## Defaults versus source definitions

The following are user choices, not facts mandated by a publisher:

- Select Uddevalla by LM municipality `1485`; spatial SCB discrepancies are findings.
- Browse existing buildings by default; retain other source statuses. Review the
  exact code list, including any “under investigation” status, before implementing
  that filter. Unknown/missing status is not automatically existing.
- Match any recorded purpose, rather than only the main purpose.
- Use component-level heights/roof form and accepted EPC links in ordinary queries.
- Allow local unpublished height evidence with its warnings; never relabel it published.
- Preserve raw failures and withhold only unsupported typed values/derived links.

The final defaults belong in named, versioned rules so they can change without
editing original inputs. Changing a rule regenerates affected derived records and
views; changing building identity or the geometry hash contract needs deeper review.

## Implementation verification

Before implementation, save/checksum the exact LM schemas/code lists and Boverket
models/workbooks, and attach section/field references to each relevant rule.
Use synthetic tests for repeated elements, bad IDs, missing addresses, conflicting
versions, geometry/hash mismatches, overlapping property parts, EPC repetition,
many-to-many links, dates/ties and SCB boundaries. Then enforce keys/constraints
and run PostGIS integration checks on the selected real source projection.

No LM relationship or EPC calculation-to-component claim may be marked verified
merely because it appears as an arrow in the UML. Unverified source interpretations
remain labelled and cannot create purportedly authoritative relationships.
