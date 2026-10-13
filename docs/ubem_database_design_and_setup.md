# UBEM Evidence Database

## Version 0.0.3

The national importer now stores source evidence once, and municipal processing
uses PostgreSQL exclusively. `db init` imports the declared source bundle;
`db init --schema-only` creates an empty schema for tests. Source evidence and
processing releases have separate identities. Municipal membership supplies each
building's primary/supporting role and selects complete certificate cohorts.

Published datasets remain immutable. A different assessment date or policy uses a
new dataset name while sharing the same source snapshot. Context assignment ranks
areas within the selected area cohort. Its dependency signature is validated
independently of loading-status flags. The following historical verification
sections record the previous development baseline; the new pipeline's acceptance
results are stored separately under `output/validation/`.

```sh
.venv/bin/sweden-ubem db init --source-bundle config/national-source-bundle.toml
.venv/bin/sweden-ubem dataset plan --municipality 1485
.venv/bin/sweden-ubem dataset process --dataset uddevalla --municipality 1485 \
  --assessment-date 2026-10-12
```

For a clean development rebuild, retain the existing database until a fresh
replacement passes the documented comparisons. Source import requires adequate
PostgreSQL-volume and host storage. Additional SCB snapshots and optional event
documents are declared in the bundle before import; municipal processing does not
acquire data or read these external files.

The database connects an LM building UUID to geometry components, heights and roof
evidence, register purposes, entrances, addresses, properties, EPCs and SCB context.
The first local dataset is all Uddevalla buildings, LM municipality `1485`.

PostgreSQL 17 and PostGIS 3.5 provide one database. Working geometry is EPSG:3006.
The optional record/EPSM API assembles models in Python from processed evidence;
its completion assumptions remain separate from imported facts. See the README
for retrieval, model receipts and simulation. Archetype/weather dependencies are
optional. Graph services and incremental source updates remain outside this
implementation. National operational validation is a separate gate.

## Buildings and components

![Building components](../output/diagrams/02-building-components.png)

A building has its LM UUID. Components retain source FIDs, versions and outline
labels. Their release-specific key is the building UUID plus a colon and the
normalized 2D geometry SHA-256. It is not a permanent physical-part identity.

Height observations must match component ID **and** geometry hash. LM outline
labels (façade or roof edge) and pipeline roof form are separate evidence. Neither
supplies roof insulation or material. There is no scalar building height/roof form.

Aggregate display and linkage geometry automatically union valid polygonal
components in the selected UUID cohort. Overlap and disconnected parts are valid;
representation labels do not trigger manual review. Overlaps count once, gaps
stay open, and original component IDs, geometry hashes and heights stay unchanged.
Invalid, empty or nonpolygonal components and conflicting municipality identities
remain explicit automatic exclusions from spatial links.

The SQLite register locates original XML records. Retain repeated purposes,
entrances, main flags and construction year, not changing present-day age.
Address points require explicit EPSG:3006 GML metadata before converting
northing/easting to PostGIS easting/northing. Unknown status/boolean values are not
guessed; the default view shows only verified existing buildings.

## Tables and identities

| Tables | One row represents |
|---|---|
| `catalog.dataset`, `dataset_layer` | National source snapshot or named selection and independent layer state |
| `catalog.processing_release`, `work_unit` | Immutable processing dependencies or atomic resumable partition |
| `catalog.asset`, `asset_location` | File hash and host/profile/path |
| `catalog.source_version`, `source_file` | Chosen content version and logical file |
| `catalog.process_run` | Operation UUID, input versions, rule/code, result |
| `catalog.field_mapping` | Regime/field, owner, type, units, review authority |
| `raw_lm.record` | National original geometry FID or XML file/record |
| `raw_height.record` | Component result or referenced measurement |
| `raw_epc.before_v260_row`, `after_v260_row` | Original flattened data-record ordinal |
| `raw_scb.record` | Boundary/grid FID or response row |
| `stock.building` | Source snapshot/LM UUID and asserted municipalities |
| `scope.dataset_building`, `dataset_certificate` | Selection membership/role or complete owned certificate |
| `stock.geometry_component` | Dataset/component ID/hash, source cohort |
| `stock.component_source_feature`, `component_relation` | Source FID lineage or component-pair decision |
| `stock.building_footprint` | Source snapshot/processing release/UUID display and linkage geometry |
| `stock.building_purpose` | Building/purpose ordinal |
| `stock.entrance`, `address_place` | Entrance/address identity; unresolved refs retained |
| `stock.property_unit`, `property_area_part`, `property_designation` | Register unit, LM part identity, qualified label |
| `evidence.component_height` | Component plus matching input geometry hash |
| `evidence.building_property` | Source snapshot/processing release/building/property union overlap |
| `evidence.epc_certificate`, `epc_subject` | Regime-qualified certificate or subject |
| `evidence.epc_observation`, `epc_candidate` | Qualified field evidence or building/subject/property decision |
| `evidence.epc_subject_result`, `epc_site_event` | Outcome for every parsed subject or published site evidence |
| `context.area`, `area_relation` | Dataset/type/code or hierarchy |
| `context.observation`, `building_area` | Area/indicator/period/dimensions or spatial assignment |
| `quality.finding` | Layer/building/operation/reason |
| `scope.definition`, `membership`, `coverage` | Saved selection or building/layer coverage |

Original records and source versions are append-only. Every consumed file is
registered directly, including XML and referenced height measurements.
XML projections preserve repeated textual values and verified file/record locators;
they are not described as byte-identical XML.
Component relations, property links, EPC candidates and subject results include
the processing release in their keys. Context assignments remain selection-owned,
with explicit references to shared source areas. Public views project the selected
dataset ID and reconstruct height/EPC provenance from original records.

## Properties and EPC

![Property and EPC](../output/diagrams/04-properties-epc.png)

Property units use register references; labels are match keys, not property
identities. A spatial overlap is not an ownership or registered-affiliation assertion.

Union footprint intersections by property unit before measuring area. Match EPC
subjects through municipality and normalized designation. The primary rule requires an exact register
house number, at least 50% overlap, exactly one eligible property and resolved
geometry. A guarded address fallback can resolve house renumbering or an omitted
address letter, with shared-address, source-warning and temporal-conflict checks.
These project rules apply to all functions; they are not LM definitions.
See [EPC matching and reporting](epc_matching_and_reporting.md) for the full rules.

Accepted/rejected candidates retain evidence and reasons. Every parsed subject
also has an outcome, including subjects with no candidates. Identity links,
declared reporting scope and historical-applicability warnings are separate. Ordinary building queries
show accepted links. Old linkage releases are comparison oracles only.

Certificates and buildings have many-to-many relations. Quantities remain on their
documented certificate/calculation owner, never copied or allocated per building.
Flattened repetition retains every raw locator without multiplying normalized
quantities. Unverified child keys remain unresolved, not invented child records.

Approval date (`ExpertGodkannande`) is identified in both Boverket mapping workbooks
as **Datum för godkännande**. Default display uses the newest verified approval date,
retaining ties and unknown dates. This is not an issue-date assertion or a
maximum-`FormularId` shortcut. The post-v260 Atemp description is inconsistent and
remains unverified. Additional mappings require explicit documented review.

The observation layer selects Atemp/use areas, floors, construction year, energy
period and performance fields, ventilation fields and approval date. Every other
source field remains in the original raw row, rather than generating hundreds of
unverified observations per certificate. No source columns are discarded.

Complete the original certificates linked to Uddevalla by retrieving buildings
touching their subjects' properties. Mark outside buildings `epc_support`; do not
follow their additional certificates. Default selection remains Uddevalla-only.
Counts describe loaded relations; source/geometry gaps prevent national-closure claims.

## SCB context

![SCB context](../output/diagrams/05-scb-context.png)

LM municipality membership selects buildings. SCB boundaries verify it but do not
change it and never participate in cadastral/EPC matching. Preserve every positive
RegSO/grid overlap. RegSO primary requires a unique point-on-surface match; grid
primary uses maximum footprint fraction. Ties remain ambiguous.

The default boundary set contains the pinned RegSO polygons and their union as
the municipality polygon. Population clipping and municipality verification use
this union. The simplified `Kommun_Sweref99TM` map is not read by the loader and
is an archived reference only. The union records its boundary year, source area
codes, snapshot and processing policy; source coordinates remain unchanged.

Statistics retain period, units, all dimensions and protected values. Population
is not building occupancy. Income keeps `tkr/year` and explicitly records the
conversion to `SEK/year`. Do not acquire outside supporting-building context.

```sh
.venv/bin/sweden-ubem source acquire-scb --municipality 1485 \
  --regso-year 2025 --income-year 2024 \
  --population-grid /Users/ssanjay/GitHub/epsm_core/ARCHIVE/EDA/befolkning_1km_2025.gpkg
```

Acquisition saves original requests/responses, metadata and checksums under
`output/sources/scb/<hash>/`. Loading never calls WFS/PxWeb. The existing Göteborg
snapshot is not Uddevalla evidence. Set `UBEM_SCB_SNAPSHOT` to choose explicitly
when several matching snapshots exist. Municipality/population load independently.

The Uddevalla snapshot acquired on 10 October 2026 is pinned in
`config/source_inventory.toml`: 18 RegSO areas for 2025 and median earned income
for 2024, both sexes. The existing population GeoPackage remains registered in place.

## Query interfaces

### Local verification, 10 October 2026

All 13 configured layers loaded into `uddevalla` on this Mac. The notebook ran
top-to-bottom, and a real three-component building passed all three export checks.
The synthetic suite passed 44 tests, including fresh PostGIS setup and backup/restore.

| Check | Result |
|---|---:|
| Building UUIDs / building-summary rows | 45,300 / 45,300 |
| Geometry components / matching height records | 47,287 / 47,287 |
| Determined / undetermined heights | 45,014 / 2,273 |
| Resolved address places | 21,120 |
| EPC certificates / parsed subjects | 3,922 / 4,063 |
| Matched / unresolved subjects | 3,972 / 91 |
| Fully matched / partly matched / unmatched certificates | 3,835 / 4 / 83 |
| RegSO areas / population cells | 18 / 537 |
| Statistical observations / building-area relations | 13,443 / 121,586 |

The earlier development database occupied approximately **1.01 GiB**; the rebuilt
database size is recorded in the evaluation JSON. Docker images, WAL,
backups and the external national source files are additional storage.

The development database was rebuilt on 12 October with the RegSO union, valid
UUID-cohort polygon unions and the general EPC identity/reporting policy. The
rebuild backup and evaluation are under `output/backups/` and
`output/validation/uddevalla-2026-10-12/`. Updated counts and limitations are
recorded in the evaluation report. Historical continuity is not established by an
accepted identity link. Heights remain unpublished staging evidence, and additional
EPC field mappings remain outstanding.

The executed notebook is saved under `output/notebooks/`; the editable notebook
remains under `notebooks/`. These counts document this local check, not a promise
of national completeness or simulation readiness.

### Views

Base views carry `dataset_id`; `_current` views use `catalog.active_dataset`.
One validated active release is selected per municipality. Python always queries
its selected dataset directly. Create a new dataset ID for a new immutable release;
already loaded layers are not overwritten.

| Views | One row represents |
|---|---|
| `mart.building_facts` | One dataset/building; no height scalar or EPC quantities |
| `building_components`, `component_heights` | Component or component observation |
| `building_addresses`, `building_properties` | Entrance or property candidate |
| `building_epc`, `epc_candidates` | Accepted relation or all candidate decisions |
| `epc_certificates`, `epc_observations` | Certificate or qualified field |
| `epc_subjects`, `epc_subject_results` | Parsed source subject or its recorded matching/scope/time outcome |
| `epc_site_events`, `epc_diagnostic_summary`, `process_runs` | Published site evidence, grouped subject counts or reproducible run |
| `context_areas`, `building_areas`, `building_context` | Area, overlap or observation relationship |
| `building_context_summary` | One dataset/building, including buildings without context |
| `scope_membership`, `scope_completeness` | Selection decision or building/layer |
| `source_inventory`, `quality_findings` | Source location or finding |

```sql
SELECT building_id, component_count, epc_certificate_count
FROM mart.building_facts
WHERE dataset_id = 'uddevalla' AND selection_role = 'primary';
```

Loading state is `not_loaded`, `loaded`, `failed` or `stale`. Evidence coverage is separately
`available`, `missing`, `failed`, `ambiguous` or `not_applicable`, with partial
component counts. There is no overall completeness score or EPSM readiness.

Saved scopes accept `purposes`, `include_support`, `existing_only` only. They match
any recorded purpose. No arbitrary SQL is stored.

## Setup and loading

![Loading](../output/diagrams/07-ingestion.png)

Acquire missing snapshots separately and declare them in the source bundle.
Initialize national evidence once, then preview and process selections entirely
inside PostgreSQL. Registration hashes files in place; source files stay external.
Seekable sources are checked before and after import. Completed source work is
reused on restart; failed output transactions roll back and an audit attempt is
recorded when the server is available. Published selections and backing source
snapshots are immutable.

```sh
docker compose up -d
export UBEM_DATABASE_URL='postgresql://ubem_owner:ubem-local-only@127.0.0.1:55432/sweden_ubem'
.venv/bin/sweden-ubem db init
.venv/bin/sweden-ubem dataset plan --municipality 1485
.venv/bin/sweden-ubem dataset process --dataset uddevalla --municipality 1485
.venv/bin/sweden-ubem dataset validate --dataset uddevalla
.venv/bin/sweden-ubem scope evaluate --dataset uddevalla
```

Use `dataset process --layers geometry register` for partial processing over
already imported evidence. `dataset plan` reports database selection/dependency
counts and performs no source reads. `db init --schema-only` initializes only the
schema for staged setup or tests. Normal `db init` imports raw evidence projections
nationally once, together with indexed typed source tables. It does not publish a
municipality automatically.

Compare recomputed links with the old EPC release without importing its relations:

```sh
.venv/bin/sweden-ubem dataset validate --dataset uddevalla \
  --compare-oracle /Users/ssanjay/GitHub/epc_join/outputs/releases/2.0.0 \
  --output output/validation/uddevalla-epc-comparison.json
```

The report records source hashes and each changed decision. Unresolved current
geometry is an explicit reason for withheld links. Other unexplained differences
remain marked for review; a comparison report is not proof of complete linkage.

### Rebuilding an older development schema

Require an exact target name and a new logical backup. Refuse external dependencies;
drop only UBEM-owned schemas, never source files or unrelated schemas.

```sh
.venv/bin/sweden-ubem db rebuild --confirm-destroy \
  --expected-database sweden_ubem \
  --backup output/backups/before-v001.dump \
  --backup-container sweden_building_combined-postgis-1
```

Use a PostgreSQL 17-compatible backup client. The Mac's PostgreSQL 15 client cannot
dump PostgreSQL 17, so the matching container client is supported after verifying
server identity. Backups have private file permissions. Test restore into a separate
database using PostgreSQL 17 `pg_restore`.

Docker binds the Mac port to localhost. Owner access initializes/rebuilds; grant
`ubem_ingest` to the loading login and `ubem_readonly` to the browsing login.
These are passwordless group roles; provision login credentials separately.
The development password is not a central-server credential.

### Height staging

The transferred catalogue contains 9,464,839 component rows, checksum:

```text
42a087f8a0fd401f90d5722656dc4612c918d7e7567c0245f77214c87a96df55
```

It remains unpublished: 76,998 await coverage and 14,779 have unresolved
representation. Publication/EPSM preflight are not ingestion gates. Match the
transferred tower geometry register and resolve measurement paths through
`height_path_mapping` or `UBEM_HEIGHT_PATH_MAP`. Preserve original blockers.

## Notebook and exports

```python
from sweden_ubem import UBEM
db = UBEM(dataset="uddevalla")
homes = db.buildings(purpose="Bostad")
building = db.building(str(homes.iloc[0].building_id))
building.components()
building.heights()
building.addresses()
building.properties()
building.epc_certificates()
building.context()
building.completeness()
db.export(homes, format="geopackage", output="output/exports/homes")
```

GeoPackage, GeoParquet and CSV exports contain related tables and shared records
once, with relationship keys, source metadata, counts and coverage warnings.
CSV geometry has WKT/CRS fields. GeoParquet has spatial metadata. Respect source
licences; an exported subset is not a complete national certificate graph.

## Verification and future deployment

Run synthetic and isolated PostGIS tests, then separately reconcile the real
selection, FIDs/XML locators, source hashes, EPC differences and the notebook.
Synthetic tests do not establish national source correctness or resolve publisher
findings. Integration tests never rebuild the working database.

USB packaging and national Linux deployment remain later tasks. Use configurable
`/srv/ubem/sources` and `/srv/ubem/app/sweden_building_combined` roots. Do not copy a
Mac virtual environment or live PostgreSQL data directory as a portable installation.
Keep the server private and test backups and restore.

## Supporting documents

- [Source paths](source_paths.md)
- [Source definitions and validation rules](validation_rules.md)
- [UML diagrams](uml.md)
- [Exploration notebook](../notebooks/explore_uddevalla.ipynb)

## Compact 0.0.4 restart

The historical migration directory is immutable. Fresh databases use the separate
`sql/baselines/0.0.4` baseline; initialization rejects legacy databases before DDL.
Completed Docker Uddevalla is preserved and restore-verified separately.

Assets/file metadata are stored once; record and component references use compact
internal IDs. Compatibility views preserve public UUIDs, component identifiers,
geometry hashes and evidence fields. LM retains typed facts, purpose/entrance/address
evidence and sparse property diagnostics; full originals are read explicitly from
verified files. EPC originals are retained once, with representative-row references.
Height catalogue facts, measurement associations and asset verification are separate;
detailed measured payloads remain in the source bundle.

Ingestion defaults to one worker, 8,192 source rows and 64 MiB serialized payload,
including child writes. Use `--batch-rows`/`--batch-bytes` or `UBEM_BATCH_ROWS`/
`UBEM_BATCH_BYTES` to lower limits. Checkpoints use committed record positions; changing
limits preserves resume. Staging tables and connections are reused. Small height assets
share bounded transactions; large assets stream through bounded pages. Processing indexes
are built after loading. Required layers, declared counts, constraints and statistics
are checked before source readiness. EOF markers do not double-count source rows.

`db status` separates source rows from materialized outputs and reports expected and
committed files/rows, substeps, elapsed time, throughput, last commits, failures and
storage. ETA appears only with a meaningful denominator. The filesystem reserve is
15 GiB; a measured full-run capacity gate precedes national import. Source completion,
Uddevalla parity acceptance and national publication are separate gates.
