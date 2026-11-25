# Unreleased — small-house height plausibility (24 November 2026)

Records of small houses (single-family type, or a register purpose starting with
`Småhus`) now carry a `height_plausibility` receipt field and `receipt.quality`
findings from `epsm_db.height_checks`:

- `small_house_height_implausible`: a component body height of 7.5 m or more
  (three or more storeys at 3 m).
- `small_house_height_exceeds_living_area`: the register living area
  (`totalBoarea`) is below 0.6 of footprint x height-implied storeys.
- `height_survey_before_construction`: the height was measured before the
  construction year. The height reader now fills `measured_at` from the
  extractor's `source_date_max` (LiDAR collection date) when it is present. The
  2026-10-13 national height baseline has no collection dates, so this finding
  stays silent there until the heights are re-extracted with strip metadata.

The cause is tree canopy: unclassified LiDAR points include vegetation, so a house
built on a wooded plot after the scan gets the canopy height. In the 2026-10-13
snapshot, 23–35 % of småhus built 2022–2026 have a body height of 7.5 m or more,
against about 1.5 % of older houses. In the SF SE.07 cohort sample, 434 of 909 kept
houses are flagged. Findings only report; no evidence is changed or dropped.

# Release 0.6.1 — community-function purposes (22 November 2026)

The register parser reads the community-function purpose element in the spelling the
Lantmäteriet XML uses (`Samhallsfunktion`) as well as `Samhällsfunktion`, and stores it
as `Samhällsfunktion`. Before this, those purposes were left out of `purposes`, while
the raw values were kept. The lake's building purposes must be re-read for
`gis.model_facts` to see them.

An existing snapshot does not need a rebuild for this: `epsm-db reparse-purposes`
reads the same building register files again and rewrites only the parsed purposes
of buildings whose stored record is otherwise unchanged. Run `epsm-db link` and
`epsm-db publish` afterwards. See `docs/gis_first_lake.md`.

# Release 0.6.0 — observed EPC systems (21 November 2026)

`epsm_db.epc_systems` reads what an EPC reports about a building's systems:
ventilation with conflicts kept, heating carriers with the same families and regime
as the measured heating target, and the heat-pump type. `observed_systems_from_record`
is meant to replace the copies in consumers, so a model and its target agree by
construction.

In-house sibling packages (epsm, epsm_data, epsm_core) are declared with lower
bounds only; the EPSM supported manifest records the tested combination.

# Release 0.5.0 — the data lake is the database (candidate, 29 October 2026)

`epsm_db` is now only the GIS-first data lake: `Lake` and `epsm-db` for
administration, `Database`, `Stock`, `StockCell`, `BuildingRecord`, `Receipt`
and `BuildingType` for Python use. The earlier `sweden_ubem` national ingestion
pipeline, its `sweden-ubem` command, the schema 0.0.4 migrations and baselines,
the native PostgreSQL and remote-runtime scripts, their notebooks, studies and
tests are retired and kept verbatim under `archive/2026-10-29/`. They are not
installed or tested. The XML register parser (`epsm_db.lm_xml`), the EPC export
encoding detection and `epsm_db.weather.WeatherReference` moved into the package.

Stock cells follow Data 0.7.0's frozen LoD2 catalogue: the fabric slot is selected
by type, era, climate zone and renovation state, while the observed ventilation
code only filters buildings. A zone-complete cell attaches an explicit
`assumption` assignment (slot, resolved-catalogue checksum, construction years)
to each record copy; a partial cell invents none. Source types outside the
authored catalogue form the `generic` cohort: the matrix keeps their counts and
their fabric assumption borrows the multifamily representative, with the donor
recorded in the assignment's `selection`. `StockCell` accepts its type and era
positionally.

Requires Data 0.7.0 (`catalogue` extra) and EPSM 0.5.0 with Core 0.9.0 (`epsm`
extra). PostGIS acceptance runs against a disposable `epsm_api_validation_*`
database named by `EPSM_LAKE_TEST_URL`.

# Release 0.4.0 — Stock workflow over the GIS lake

Canonical `Database`, lazy `Stock`/`StockCell`, portable `BuildingRecord`, and
explicit `to_epsm()` now use the GIS lake. Bounded GeoDataFrame previews expose
truncation receipts; complete iteration and scoped GeoPackage extracts share
selection predicates. Records retain all component heights, ambiguous EPCs,
unresolved property parts, area memberships and source provenance.

EPSM conversion targets EPSM 0.3.0, Core 0.4.0 and Data 0.6.0. Archetype application
is explicit (`archetype="cell"` or a supplied Data Archetype), with no geometry
hash joins, storey inference, certificate allocation or simulation execution.
Portable `epsm-record-1` exchange and coordinated standard logging are retained.

Lake schema version 2 is an additive `epsm-db migrate` extension for version 1;
run `publish` afterwards. Models require coherent source/link/GIS/model revisions.
The existing ingestion runtime should finish before live migration. Legacy
baseline database conversion, promotion and retirement remain separate.

See [the stock API](docs/stock_api.md) for migration, diagrams and examples.

# Release 0.3.0 — GIS-first lake branch, 16 October 2026

`epsm-db` now uses a fresh GIS lake schema with ordinary numeric keys, streaming
source imports, explicit evidence links and indexed materialized GIS layers.
The source kinds are components, building registry, properties, addresses, EPC
records and heights, plus versioned areas, statistics and NVDB roads. Boundary
edition and observation period remain separate; building-area links retain
ambiguous memberships. GeoPackage ZIP exports retain NVDB layer/member identity
and original attributes. Import, link and publish are independent commands; `run`
combines them. Geometry/height browsing has no dependency on municipal acceptance
or certificate-by-certificate spatial matching.

The Python `Lake` API administers the new schema and provides bounded feature
queries. Original geometry hashes are never calculated or used as relational
keys. Prepared upstream component IDs are retained as opaque source references
so existing height catalogues can be read without rehashing geometry.

**Command compatibility:** use `sweden-ubem` for existing schema 0.0.4 operations.
The new `epsm-db` requires a fresh database. Existing `Database`, `Stock` and
`BuildingRecord` imports retain legacy behavior. No automatic production cutover
or in-place migration is included. No tests or performance benchmarks have been
run for this branch.

See [the lake guide](docs/gis_first_lake.md) for model, commands and limitations.

# Release 0.3.0 — 15 October 2026

Stock and exploration catalogue inspection use the public Data Catalogue APIs, retaining classifications, incomplete drafts and source evidence. Physical Building assembly stays in EPSM.

The catalogue extra requires Data 0.6.0 or later; the EPSM extra requires Core 0.4.0 and Data 0.6.0 while retaining the compatible EPSM 0.2.0 minimum. See [the catalogue consumer guide](docs/catalogue_inspection.md).

This package version does not change the physical schema baseline, evidence contract or dataset release identity. No live database migration, deployment change or national ingestion is part of the release.


# Release 0.2.0 — 15 October 2026

Record retrieval, stock conversion, ingestion, processing, validation, exports and throttled progress. The sweden_ubem logger remains supported; database schema stays 0.0.4.

Libraries emit through Python's standard logging without configuring output.
Instrumented operations accept optional keyword-only `log_context`; identifiers
stay separate from scientific inputs, saved models and cache keys. INFO shows
major stages; DEBUG adds decisions and timing. Standalone commands support
`--log-level`, including `OFF`, where the package provides a command.

The EPSM application configures the combined stderr and optional DEBUG JSONL
output with `configure_logging`; `enabled=False` disables both destinations.
Existing callers and supported older collaborator signatures remain supported.
No new mandatory dependency is introduced by the logging implementation.

The coordinated release was requested for manual review and local validation
because GitHub refused to start several hosted jobs due to account billing.
Hosted checks that did not run are not counted as passes. See the
[coordinated verification record](https://github.com/SB-Chalmers/epsm_development/blob/main/docs/logging-verification.md)
for exact tested revisions, executed checks and remaining limitations.
