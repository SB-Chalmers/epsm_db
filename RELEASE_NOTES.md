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
