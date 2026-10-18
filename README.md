# epsm_db — GIS-first building data lake

Version **0.4.0** provides the stock Python workflow over a simpler PostgreSQL/PostGIS lake for linked property,
building, component, address, EPC and height evidence, with versioned boundaries,
SCB statistics and NVDB road data.

**Import sources → link entities → publish GIS layers.**

- Ordinary numeric keys and original source identifiers; no per-geometry hashing.
- Streaming COPY imports with file checkpoints and resumable bounded batches.
- Explicit relationships, including ambiguous/unmatched evidence.
- Ready-to-use layers with typed EPSG:3006 geometry, spatial indexes, stable feature
  IDs and prepared attributes. Panning does not repeat the evidence joins.
- Geometry and available height evidence can be published independently of EPC
  matching, modelling, acceptance runs and enrichment.

## Start a fresh lake

Requires Python 3.11+, PostgreSQL 15+ and PostGIS. Create a fresh database owned by
its ingestion administrator; use `.pgpass` or your local credential mechanism.

```bash
python -m pip install -e .
export EPSM_DATABASE_URL='postgresql://ubem_owner@127.0.0.1:55433/epsm_gis'
epsm-db init
epsm-db ingest config/gis-lake.toml --source-root /path/to/source-bundle \
  --kinds components heights
epsm-db link
epsm-db publish
epsm-db status
```

Add building registry, address, property and EPC sources later with
`--kinds buildings addresses properties epc`, then link and publish again.
`epsm-db run config/gis-lake.toml --source-root /path/to/source-bundle` performs
the complete workflow. The [GIS lake guide](docs/gis_first_lake.md) explains
source adapters, matching rules, publication freshness and access.

## Use in QGIS or Python

Connect to the database and expand **gis**. Drag **buildings**, **component_heights**,
**properties**, **addresses**, **building_properties**, **epc_links**, **boundaries**
or **roads** onto the map.
Missing evidence and uncertain links have explicit status fields.

```python
from epsm_db import Lake
rows = Lake().features('component_heights', municipality_code='1485', limit=1000)
```

A database administrator can grant existing reader roles access using
`epsm-db grant ubem_readonly`. The map layers are materialized views with spatial
indexes; `components` and `footprints` are convenient aliases. Full SQL remains
available for detailed relational queries and analysis.

## Boundary and road sources

The [updated model diagram](docs/gis_first_lake.md#model) includes country, county,
municipality, RegSO, DeSO, SCB grids, statistical observations and NVDB road data.
Statistics retain both observation period and boundary edition. Buildings link to
areas; NVDB features retain network references and their original attributes.

The Mac's `DeSO_2025.gpkg` and NVDB `geopackage.zip` sources are declared in
`config/gis-context.toml`. See [transfer and import instructions](docs/mac_gis_sources.md).
Declarations alone do not load sources; inspect the deployment coverage before importing.

## Stock Python API

```python
from epsm_db import Database

db = Database("service=epsm_lake")
stock = db.stock(municipality="1485")
cell = stock.cell(building_type="multi_family", era="SE.02")
frame = cell.frame()  # Bounded GeoDataFrame; receipt in frame.attrs["receipt"]
for record in cell.iter_records(batch_size=100):
    print(record.building_id)
buildings, receipts = cell.to_epsm()  # Evidence-only drafts
buildings, receipts = cell.to_epsm(archetype="cell")  # Explicit assumptions
cell.extract("selected-buildings.gpkg")  # Complete selection
```

See [the stock API and EPSM model diagram](docs/stock_api.md) for current Data/Core
integration, receipts, portable records, geographic filters and complete extracts.

## Existing databases

`Database`, `Stock` and `BuildingRecord` now target GIS lake schema version 2.
Existing version-1 GIS lakes use `epsm-db migrate`, then `epsm-db publish` after
the national build finishes. This extends the lake without source reingestion.
Legacy schema 0.0.4 databases retain their separate `sweden-ubem` command.

See the [legacy workflow](docs/legacy_workflow.md),
[existing remote user access](docs/remote_user_access.md), and
[GIS-first design and rollout status](docs/gis_first_lake.md).

The separate `epsm_lake` national database finished ingestion and publication on
17 October 2026. The version-0.4.0 stock API is deployed on schema version 2.
Registered readers can use QGIS and Python through Tailscale. National performance
benchmarking and database promotion remain separate; the old database is retained.

## Live candidate lake and area extracts

See [GIS lake access](docs/gis_lake_access.md) for QGIS/Python connection settings,
GeoPackage extracts, supplied source coverage and build monitoring.

[Automatic SCB downloads](docs/scb_open_data.md) add national population, household,
housing, income, employment, education, vehicle and energy statistics to the same
area/statistics model.
