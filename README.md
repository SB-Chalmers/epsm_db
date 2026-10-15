# epsm_db — UBEM Evidence Database

Python package version **0.1.0**; database schema **0.0.4**. Tested on PostgreSQL 15 and 17 / PostGIS 3.5. EPSG:3006.

Initialize shared national LM, height, property, address, EPC and available SCB
evidence once. Process Uddevalla (`1485`), another municipality, or Sweden through
internal database queries. Original source bytes live in the portable, Git-excluded `data/raw/national-2026-10-13/` bundle.
Source import, model processing, and publication have separate completion states.
Optional record retrieval and EPSM adaptation use processed evidence. Source import
does not prepare simulations or automatically acquire weather.

On this Apple Silicon host, use native PostgreSQL **17.11** with PostGIS **3.5.2**
on port **55433**. The dedicated cluster is `output/runtime/native-pg17-v004/data`;
its durable settings are in `config/native-postgresql17.conf`. Start an installed
cluster with `sh scripts/start_native_postgres.sh`. The original Docker database
on port 55432 remains the protected Uddevalla reference. Native execution improves
some workloads; national completion and functional parity require their own checks.
For a fresh Mac installation, `sh scripts/build_native_postgis17.sh` builds the
checksum-pinned PostGIS source against Homebrew PostgreSQL 17. It leaves existing
PostgreSQL services intact. The cluster startup script creates the dedicated
development database and local credentials on its first run.

## Existing Uddevalla

The completed database remains available independently of the national rebuild:

```sh
sh scripts/start_uddevalla_notebook.sh
```

The launcher selects the completed database and uses a read-only connection.
It works independently of the replacement database on port 55433.

Its self-contained preservation set is `output/retained/uddevalla-2026-10-12/`.
The retained dump was restored and all 29 public projections, including full
geometry and field mappings, matched both the baseline and the live database.

A reusable [stock exploration notebook](notebooks/explore_stock.ipynb) shows catalogue cells, evidence inspection and heating plots. Configure a read-only `UBEM_DATABASE_URL` before running it. New code imports `Database`, `Stock` and `BuildingRecord` from `epsm_db`. Existing `sweden_ubem` imports and commands remain available for compatibility.

## Setup

```sh
python3.11 -m venv .venv
.venv/bin/pip install -e '.[test,notebook]'
sh scripts/start_native_postgres.sh
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
export UBEM_DATABASE_URL='postgresql://ubem_owner:ubem-local-only@127.0.0.1:55433/sweden_ubem_v004'
export UBEM_CAPACITY_GUARD_PATH="$PWD"
export UBEM_MIN_FREE_BYTES=16106127360
.venv/bin/sweden-ubem db init --source-bundle config/national-source-bundle.toml
# In a second terminal, inspect committed progress and storage during import:
.venv/bin/sweden-ubem db status
.venv/bin/sweden-ubem dataset plan --municipality 1485
.venv/bin/sweden-ubem dataset process --dataset uddevalla --municipality 1485 \
  --assessment-date 2026-10-12
.venv/bin/sweden-ubem dataset validate --dataset uddevalla
.venv/bin/sweden-ubem dataset report-epc --dataset uddevalla --output output/validation/epc-report
```

`db init` performs the national import; this is more work than initializing an empty
schema. Repeating an identical completed import is a no-op. Interrupted source
imports resume committed work without duplicating records. `db init --schema-only`
creates the empty schema for synthetic tests. The previous development schema is
rejected: capture a baseline and use the backed-up, target-confirmed rebuild in the
[manual](docs/ubem_database_design_and_setup.md), or use a separate fresh database.

`source inspect --record-id ID --database-url URL --source-root ROOT` reads the
verified original file using its registered locator. Missing or changed assets
raise an error. Ordinary processing, reports, exploration and exports use database
facts and need no raw-file access.

`db status` reports source import separately from municipality/national selections,
committed work by substep, and table/index disk usage. A source layer remains
`not_loaded` until its full import and reconciliation checks finish. Disk usage is
not RAM usage; partial import size does not estimate the final national footprint.

Source locations use bundle-relative paths in `config/source_inventory.toml`.
`--source-root` overrides `UBEM_SOURCE_ROOT`; both override the repository bundle.
Moving the bundle requires changing only that root; there is no neighboring-project fallback.
The bundle pins imported SCB snapshots and optional site-event JSON documents.
Acquire additional snapshots separately before initialization. Current RegSO/income
coverage is Uddevalla; national population coverage does not imply national income
coverage. Height evidence remains unpublished, with unresolved source coverage.
Verify storage capacity on the PostgreSQL volume and its host before national import.
For the native cluster, set `UBEM_CAPACITY_GUARD_PATH` to the filesystem holding
its data directory. For Docker, use the filesystem holding its disk image.
The importer checks free space before each source batch and height
measurement file, with a default 15 GiB reserve (`UBEM_MIN_FREE_BYTES`). This guard
does not measure a remote database volume. Event documents and SCB snapshots resolve through the same source bundle. Source field mappings are frozen for the backing
snapshot; changing them requires a fresh import so normalized values cannot become
stale. The fresh 0.0.4 baseline retains the verified after-v2.6 certificate-owned
Atemp mapping against Boverket's v2.8 form (m² excluding warm garages).
Historical migrations remain unchanged; initialization refuses an in-place upgrade. Storey/area screening lives in `physical_assessment.py`
and never rewrites source facts or allocates shared certificate quantities.

Municipal processing never reads source files. Complete building UUID cohorts and
certificates are preserved. Shared addresses, property parts and certificate
quantities are stored once. Exact property/house identity and guarded address
fallback use the same policy for all functions. All candidate buildings of selected
certificates are inspectable as support; their unrelated certificates do not enter
the selection. Defaults remain primary buildings with verified existing status.

```python
from epsm_db import Database

db = Database(dataset="uddevalla")  # Opens the database to verify this dataset
db.archetype_matrix()  # All buildings by EPSM category and register-year era
db.archetype_matrix(building_type="multi_family")  # One category
db.ventilation_counts(building_type="multi_family", era="SE.01")  # Reported systems
db.ventilation_buildings(building_type="multi_family", era="SE.01")  # Building IDs
homes = db.buildings(purpose="Bostad")
building = db.pick(multi_component=True)
building.components()
building.heights()
building.epc_certificates()
db.completeness()
```

`archetype_matrix()` and `stock()` require the optional `epsm_data` package (`pip install "epsm_db[catalogue]"`). It includes
buildings without EPCs and reports missing register years in a separate column.
`ventilation_counts()` separates reported S/F/FT/FTX from missing or unresolved
EPC ventilation evidence; it does not count model defaults as reports.

For stock exploration, keep the catalogue and building records together:

```python
stock = db.stock()  # Lazy database-backed collection with stock.catalogue
stock.matrix()      # Counts by building type and register-year era
cell = stock.cell(building_type="multi_family", era="SE.01", ventilation="S")
len(cell)           # All matching buildings, with no EPC availability filter
cell.frame()        # Common building properties as a table
cell.component_frame()  # One row per footprint component, with its own height and roof
cell.epc_heating()  # Reported heating and explicit exclusions
cell.plot_heating() # EPC reported heating histogram (or per_area=True)
cell.plot_atemp()   # Reported heated-area histogram
for record in cell:
    print(record.building_id, record.facts, record.components)
    break
```

`stock` and its cells fetch records in bounded batches. A full EPSM catalogue slot
also needs an explicit `climate_zone` and `ventilation`. SE.01–SE.06 default
to the `existing` renovation state; SE.07 covers 2022 onward and accepts no
renovation state. Then
`cell.archetype_slot` resolves the slot.
`models, receipts = cell.to_epsm(storeys=3, storey_height_m=2.8)` authors physical
`epsm.Building` drafts and returns a receipt for every attempted building. These
example dimensions are caller assumptions. Missing inputs leave an inspectable
draft; invalid inputs leave a failure receipt. Supply weather only to a later
`building.simulate(...)` call. The previous weather/policy-based conversion is
now `cell.to_epsm_legacy(weather=...)`; its weather can be a reference, a
UUID-to-reference mapping, or a callable. Catalogue zone and
renovation choices are model assumptions, not observed database classifications.
Fully specified cells return independent record copies whose `archetype_assignments`
retain the selected slot, category, period, state, source and catalogue content hash.
Partial cells leave assignments unresolved. These links do not apply constructions
to a Building automatically; model assembly makes that selection explicitly.
`stock.plot_heating(building_type=..., era=..., ventilation=...)` and
`stock.plot_atemp(...)` are convenience forms. Stock and cell objects have concise
representations in notebooks; `sweden_ubem.stock` emits optional debug logs when
configured through Python's standard `logging` module.

`cell.frame()` includes EPC reported storeys and basement storeys, plus geometry
matched representative, eave, and ridge heights and roof form. Height and roof
columns are populated at building level only when the building has one footprint
component; use `cell.component_frame()` to inspect each part of a multi-component
building. Missing or mismatched height evidence stays missing. In
`cell.epc_heating()`, `reported_heating_basis` distinguishes the newer EPC's
separate `UPPV` space-heating fields from the older form's combined carrier amount
minus `EgiVVBered` hot-water preparation. The older form's two operands are shown
as `combined_heating_dhw_kwh` and `dhw_subtracted_kwh`; unresolved cases have an
explicit exclusion. These are reported delivered energy amounts, not simulated
thermal demand.

Use `include_support=True` or `include_other_statuses=True` to inspect supporting
buildings and other/unknown statuses. Each published selection pins its source
snapshot, processing policies, code, mappings, assessment date and site evidence.
Use a new dataset name to publish changed rules or dates. National selections use
`dataset process --dataset sweden --national`; incomplete national context remains
explicit and prevents a false complete-publication claim.

- [National ingestion implementation plan](docs/national_ingestion_reimplementation_plan.md)
- [Measured restart results and rollout gates](docs/national_restart_results_2026-10-13.md)
- [Design and operations manual](docs/ubem_database_design_and_setup.md)
- [Source paths](docs/source_paths.md)
- [Source definitions and checks](docs/validation_rules.md)
- [EPC matching and reporting](docs/epc_matching_and_reporting.md)
- [Exploration notebook](notebooks/explore_uddevalla.ipynb)

## Retrieve records and assemble EPSM models

```python
from sweden_ubem.io import fetch_record, fetch_records
from sweden_ubem.enums import BuildingType

record = fetch_record.from_uuid(
    "ee1d78d0-4305-4cba-b7b4-e1ee0614799f", dataset="uddevalla"
)
print(record.type)
print(record.receipt.completeness)
record = fetch_record.from_point(x=321255.6, y=6474488.2, epsg=3006,
                                dataset="uddevalla", min_footprint_area_m2=100)
record = fetch_record.from_address("Skillinggränd 19", municipality="1485",
                                  dataset="uddevalla")
```

Set `UBEM_DATABASE_URL` or supply `database_url` per call. Bind repeated queries
with `reader = fetch_records.using(database_url, dataset="uddevalla")`.
Defaults use `UBEM_DATASET`, otherwise `uddevalla`. Select `sweden` explicitly
when that processed dataset exists; backing national source datasets are rejected.
Every request uses a read-only repeatable-read transaction.

Singular lookups require a unique result; `AmbiguousRecordError.candidates` exposes
the candidates. UUID lookup includes supporting/other-status inventory by default.
Spatial collections default to primary, verified existing buildings. Filters include
`include_type`, `has_epc`, `construction_year=(1960, 1980)`, minimum/maximum footprint
area, `include_support`, and `include_other_statuses`.

```python
records = fetch_records.within_boundary(
    polygon, epsg=3006, dataset="uddevalla", spatial_match="intersects",
    min_footprint_area_m2=100,
    include_type=[BuildingType.SCHOOL, BuildingType.RESIDENTIAL_MULTIFAMILY],
)
```

Boundary predicates are `intersects`, `covered_by` and `centroid_within`.
Coordinates are `(x, y)`; EPSG:4326 uses longitude, latitude. Polygon inputs accept
Shapely, WKT or GeoJSON. Selectors also include `from_municipality` (code/name),
`from_regso`, `from_deso`, `from_property` (designation or `property_uuid`),
`from_bbox`, `within_radius` and `nearest`. `from_footprint` uses intersection-over-
union with an explicit minimum fraction, default 0.8. `nearest` returns distance-
ordered candidates within `max_distance_m`; it rejects regions exceeding 10,000
candidates. `record.neighbours(radius_m=100)` keeps its connection/dataset.
Municipality names resolve through a bundled SCB 2026-02-25 name/code catalogue
before querying the selected database, including labels such as `Uddevalla kommun`.

Selections return at most 1,000 records by default (maximum 10,000). If
`records.receipt.truncated`, repeat the query with
`after_uuid=records.receipt.next_cursor`. Missing RegSO/DeSO geometry raises
`BoundaryUnavailableError`, not an empty-building claim; DeSO is not currently
imported. Receipts expose incomplete dataset layers. Detailed types combine LM
purpose and scope-qualified latest EPC shares; broad business use alone is not office.

EPSM integration is optional. Install local checkouts into the same environment,
or use `.[epsm]` when these package versions are available:

```sh
.venv/bin/pip install -e ../epsm_data -e ../epsm_core -e ../epsm_lca -e ../epsm
export ENERGYPLUS_EXE=/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus
```

The current interface authors a physical Building without opening another database connection:

```python
import json
from epsm import Building
from epsm_db import BuildingRecord

payload = json.loads(json.dumps(record.to_dict()))
restored = BuildingRecord.from_dict(payload)
assert restored._database_url is None  # Retrieval credentials are excluded
building = Building.from_record(restored, storey_height_m=2.8)
print(building.readiness())
```

The explicit storey height is a modelling assumption; the adapter preserves source
observations separately. Incomplete records produce an incomplete Building.
Record and receipt `repr()`/`str()` are bounded, single-line summaries. Use
`record.summary()` or `receipt.summary()` for loaded metadata, and
`print(receipt.report())` for the detailed completeness table and findings.
These inspection methods do not query the database.
Portable records retain component and address geometries, certificate dates,
observations, quality findings and receipt metadata in EPSG:3006. They do not retain
the source database connection. Explicit subsequent queries require a configured
connection. `record.to_epsm(...)` forwards to the same `Building.from_record` interface.

The earlier adapter remains available for existing studies; its assembly now lives
in `epsm`, and the following example uses that compatibility interface:

```python
from sweden_ubem.epsm import EPSMBuilding, ModelPolicy
from sweden_ubem.weather import WeatherReference

weather = WeatherReference.from_epw("/path/to/weather.epw", calendar_year=2023)
building, receipt = EPSMBuilding.from_record(record, weather=weather)
print(receipt.completeness)
print(receipt.assumptions)
print(receipt.ready_for_simulation)
building.type                    # BuildingType
building.spec                    # native epsm_core.BuildingSpec
building.write("output/model/building.json")  # writes receipt too; refuses overwrite
# result = building.simulate("output/model/run")

buildings, receipts = EPSMBuilding.from_records(records, weather=weather)
failures = [r for r in receipts if r.errors]   # one receipt per attempted building
```

For geographically varied batches, `weather` can be a UUID-to-reference dictionary
or a callable `weather(record)` that returns a preassigned reference, for example
a cached file selected by municipality. Weather selection is explicit and source-pinned.

`record.to_epsm(**building_options)` uses the physical authoring route.
The older `UBEM(...).building(uuid).to_epsm(weather=...)` spelling remains a
legacy compatibility interface; use `.record()` followed by the public
`Building.from_record(...)` for new work.

`ubem_v1` preserves footprints and uses eaves/ridges for inferred cold-attic gables.
All components share a local metric origin during model assembly. Receipts retain
the EPSG:3006 translation origin and inverse transform, preserving orientation and
relative placement while avoiding coordinate-dependent roof splitting.
Unclear roofs become flat with an assumption. Credible reported storeys use a
**permissive 2–5 m full-floor screen**; missing/suspect counts are inferred with
3 m typical floors and unchanged body height. This differs from the exploratory
2.5–3.5 m check and is not an accuracy gate. Basements support zero/one full heated
floorplate, assumed 2.5 m depth and 18 C setpoint. Unknown presence defaults to none
with an assumption. Multiple basements and unresolved geometry produce failure
receipts. EPC programme shares below 5% are assigned to the largest remaining use
before blending; exactly 5% remains in the blend. Unsupported uses at or above 5%
receive Core's `epsm.generic_other.v1` daytime screening profile. Receipts retain
reported and modelled shares, folded fractions, and fallback quality flags. The
generic profile is an explicit assumption and should be replaced when actual use
is known. Core's terrain/partial-basement/occupied-roof limitations remain
explicit approximations.

`ModelPolicy` configures these assumptions, WWR (currently 0.2 for all four types),
ventilation defaults, FTX recovery, attic exchange, delivery and climate zone.
**SE.Zone3 is a declared default catalogue classification, not national geographic
inference**; set `overrides={"climate_zone": ...}` or a custom policy as appropriate.
Year uses the register, then a scope-qualified EPC; missing years require an override.
Storeys, basement count, year, ventilation, WWR and climate zone accept overrides.

Atemp affects operational reference area, not surfaces or air volume. Missing or
shared Atemp uses geometric conditioned area as a labelled operational fallback.
Records retain raw values, alternatives and mapping status. Only a unique latest
verified-dated, fully matched single-subject/single-building certificate supplies
individual quantities. Fractional residential/office/school programmes use native
`ProgramBlend`, primary-use controls and common ventilation. Unsupported positive
shares fail; missing shares use a declared primary-use scenario. Matrix `existing`
constructions and office/school calendars remain assumptions.
For an explicit envelope scenario, pass
`overrides={"renovation_state": "standard"}` or `"advanced"` to
`EPSMBuilding.from_record`. This selects the EPSM Data package for fabric,
glazing and infiltration while retaining geometry and operational inputs; the
selected state is recorded as a scenario, not inferred renovation evidence.
Residential DHW uses Core's type-specific historical normal-use profiles: SF and
MF remain distinct. Initial models have no neighbouring shading/party-wall
assignments; use the native Core context/contact interfaces when those are available.

The optional dwelling-zoning experiment in `examples/dwelling_zone_experiment.py`
requires physical dwelling and service-space walls. Its compiler verifies reciprocal
wall faces with thickness, conductivity and thermal mass, and rejects AirBoundary
constructions. The current 150 mm concrete assembly is a declared experimental
assumption; detailed dwelling zoning is still separate from the adapter's per-storey model.

Models use Data's volume/area-aware infiltration handoff and Core Kiva foundations.
A single indicated carrier gets declared effective delivery assumptions; mixed or
unknown carriers retain ideal thermal demand without purchased-energy conversion.
Do not compare ideal demand with total EPC energy. Native schema validation and
compilation preflight run by default; `preflight=False` does not claim simulation
readiness. Compilation success is not empirical energy validation.
`ModelAssemblyError.receipt` retains errors and assumptions.

`WeatherReference.from_location(latitude=..., longitude=..., year=..., directory=...)`
explicitly calls optional `smhi2epw`, caching a checksum-checked artifact. Retrieval
and assembly normally use existing EPWs without network access. Weather years and
EPC normalization/measurement periods remain separate choices.
Install `.[weather]` or the local `smhi2epw` checkout to enable generation. For a
building-specific location, use
`WeatherReference.from_location(**record.location, year=2023, directory="output/weather")`.
For EPC heating validation, `sweden_ubem.epc_heating.heating_target_from_record(record)`
retains two distinct values: source-form delivered heating over the reported
12-month measurement period and, where the post-v2.6 form reconciles exactly,
an inferred normal-year delivered-heating target. Match the former to AMY
simulations for its exact months, using `period_heating_kwh` on each calendar
year's hourly carrier input. The latter belongs to a normal-year comparison;
it is not an AMY target. Both retain raw fields, mapping status and exclusions.
The reproducible Uddevalla panel runner is
[`scripts/compare_uddevalla_epc_amy.py`](scripts/compare_uddevalla_epc_amy.py);
it records the common AMY reference point and each building's distance to it.
`building.widget()`/`building.view()` expose Core geometry inspection.

The runnable [example](examples/postgres_to_epsm.py) retrieves, compiles and writes
one model; add `--simulate` to run EnergyPlus:

```sh
.venv/bin/python examples/postgres_to_epsm.py \
  --uuid ee1d78d0-4305-4cba-b7b4-e1ee0614799f --dataset uddevalla \
  --epw ../epsm_core/data/weather/SWE_VG_Gothenburg-Landvetter.AP.025260_TMYx.2004-2018.epw \
  --year 2023 --output output/my-building
```

## Tests

```sh
PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH" \
UBEM_TEST_ADMIN_URL='postgresql://ubem_owner:ubem-local-only@127.0.0.1:55433/postgres' \
UBEM_TEST_BACKUP_CONTAINER='' \
UBEM_TEST_PG_RESTORE=/opt/homebrew/opt/postgresql@17/bin/pg_restore .venv/bin/pytest -q
```

Integration tests create and remove uniquely named disposable databases. They never
rebuild the working database. Synthetic checks cover national-once import,
database-only processing, repeated initialization, restartability, complete
certificates, immutable publication, read-only interfaces, notebook execution,
exports and backup/restore. Real-source parity and national capacity measurements
are separate acceptance gates; a passing synthetic suite does not establish UBEM
simulation readiness.

## Harmonised Python interface

The distribution is now `epsm_db`. Use `from epsm_db import Database, Stock,
BuildingRecord`. Existing `sweden_ubem` imports and command remain available.
There is one Stock and one BuildingRecord implementation. Ingestion and live
database schemas are unchanged. `record.to_dict()` preserves evidence and
geometry without credentials; `BuildingRecord.from_dict()` reconstructs a
disconnected record. `record.to_epsm()` delegates to `epsm.Building.from_record`.
SE.07 covers 2022 onward and accepts no renovation-state selector.

## Stock conversion migration

`cell.to_epsm()` now matches `record.to_epsm()`: it returns physical Building
objects with explicit missing inputs, without running preflight compilation or
selecting weather. It keeps the `(buildings, receipts)` structure; identify each
model by `identifier` and each receipt by `building_id`. Invalid records have a
receipt without a model. Source receipts and catalogue assignments are retained
independently, and no catalogue choice is applied automatically.

Move existing `weather`, `policy`, `overrides`, `preflight` and `energyplus`
arguments to `cell.to_epsm_legacy(...)` to retain the previous BuildingSpec route.
A complete physical readiness receipt does not prove engine execution or
scientific accuracy.

Installed wheels include the owned SQL baselines and source inventory under the
installation prefix’s `share/epsm_db`. Source checkouts retain their existing
`sql/` and `config/` layout. External national inputs remain separate; select
their root explicitly with `UBEM_SOURCE_ROOT` when using an installed package.

## Operation logging

Library calls use Python's standard `logging` module and install no console or
file output. Instrumented operations accept optional keyword-only `log_context`
with small identifier mappings; nested events carry operation and parent IDs.
INFO describes major stages; DEBUG exposes selections, conversions and caches.
Logging context does not change physical models or scientific cache inputs.

For an application using EPSM's coordinated logging setup:

```python
from epsm import configure_logging, reset_logging

configure_logging(directory="outputs/example")  # INFO stderr, DEBUG JSONL
# Run operations here.
configure_logging(enabled=False)  # Stop console and file logging.
reset_logging()  # Restore the host application's settings.
```

EPSM is needed only for this combined configuration, not for ordinary library
logging. Existing scripts can keep using `logging.basicConfig(level=logging.INFO)`.

The standalone command accepts `--log-level` before its subcommand, including `OFF`. Command results remain on stdout.
