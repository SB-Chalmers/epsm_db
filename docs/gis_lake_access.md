# Connect to the GIS-first lake

The replacement database is `epsm_lake`. It is a candidate for the main lake;
its build is independent of the retired `sweden_ubem_v004_baseline` database.
Source rows become visible in map layers at publication stages. Inspect
`gis.publication_status` and `gis.sources` for freshness and incomplete files.
An empty layer during the build does not mean its national source is empty.

## QGIS

Keep Tailscale connected, then create a PostgreSQL connection in QGIS:

| Setting | Value |
| --- | --- |
| Name | EPSM GIS lake |
| Host | `csd-lab-precision-7540.tailb64966.ts.net` |
| Port | `55433` |
| Database | `epsm_lake` |
| Username | Your registered CID, for example `ssanjay` |
| Password | Your existing PostgreSQL password |
| SSL mode | Disable (the connection is carried over private Tailscale) |

Open the **gis** schema. The primary key is **fid** and the geometry column is
**geom**, in **EPSG:3006**. These typed, indexed snapshots support normal QGIS
browsing and bounding-box requests:

- `gis.buildings` / `gis.footprints`: building footprints and evidence counts.
- `gis.component_heights`: individual footprints with measured height evidence.
- `gis.properties`, `gis.addresses`, `gis.epc_links`: linked domain layers.
- `gis.deso`, `gis.regso`, `gis.municipalities`, `gis.counties`,
  `gis.national_boundary`, `gis.population_grids`: geography layers.
- `gis.roads`: both NVDB themes; filter `source_layer` to choose road widths or
  network classification. Original `Bredd`/`Nattyp` fields are in `attributes`.

Use **Save Features As → GeoPackage** in QGIS to export a filtered layer. For a
bundle of layers selected by municipality or area, use the Python/CLI extractor.
The database metadata and registered-role reads were checked on the server;
the QGIS desktop UI has not been exercised on the Mac during this setup.

## Python

The national lake and stock API are published at revision **10978**, schema version
**2** (deployment completed 17 October 2026). Connection settings and the `ssanjay`
reader role were checked on 18 October 2026. New source imports can advance this
revision; consult `gis.publication_status` for the current state.

### Install the branch on your Mac

With your intended Python environment activated:

```bash
cd /Users/ssanjay/GitHub/epsm_db
git fetch origin
git switch codex/gis-first-data-lake
git pull --ff-only origin codex/gis-first-data-lake
python -m pip install -e .
```

For a new checkout instead:

```bash
git clone --branch codex/gis-first-data-lake https://github.com/SB-Chalmers/epsm_db.git
cd epsm_db
python -m pip install -e .
```

GitHub authentication is needed for this private repository. Database login uses
your separate registered PostgreSQL CID and password.

A wheel is also available without a Git checkout. Run on the Mac:

```bash
scp 'csd-lab@csd-lab-precision-7540.tailb64966.ts.net:/home/csd-lab/ubem/runs/gis-lake-2026-10-16/dist-stock-api/epsm_db-0.4.0-py3-none-any.whl' .
python -m pip install ./epsm_db-0.4.0-py3-none-any.whl
```

### Stock API

```python
from getpass import getpass
from psycopg.conninfo import make_conninfo
from epsm_db import Database

db = Database(make_conninfo(
    host="csd-lab-precision-7540.tailb64966.ts.net",
    port=55433,
    dbname="epsm_lake",
    user="ssanjay",
    password=getpass("Database password: "),
    sslmode="disable",
    connect_timeout=10,
))

buildings = db.stock(municipality="Göteborg").frame(limit=1000)
# Preview truncation and publication are recorded in buildings.attrs["receipt"].
```

Keep Tailscale connected. QGIS and Python connect directly to the private database
endpoint; no SSH tunnel is needed. For Data catalogue cells and EPSM conversion,
use the coordinated optional packages described in [the stock API](stock_api.md).

### GIS features and geographic extracts

```python
from getpass import getpass
from psycopg.conninfo import make_conninfo
from epsm_db import Lake

lake = Lake(make_conninfo(
    host="csd-lab-precision-7540.tailb64966.ts.net",
    port=55433,
    dbname="epsm_lake",
    user="ssanjay",  # use your CID
    password=getpass("Database password: "),
    sslmode="disable",
))

# A bounded read, suitable for notebooks. Coordinates are EPSG:3006 metres.
rows = lake.features("component_heights", municipality_code="1485", limit=1000)

# A complete municipality extract, streamed in batches to a GeoPackage.
# No hidden 1,000/100,000-row limit applies to extracts.
lake.extract("uddevalla.gpkg", municipality_code="1485")

# Or select an individual statistical area, explicitly choosing its edition.
lake.extract("deso.gpkg", area_type="deso", area_code="0114A0010",
             boundary_version="2025", layers=["buildings", "component_heights"])

# Or select an arbitrary rectangle in EPSG:3006.
lake.extract("study-area.gpkg", bbox=(315000, 6470000, 325000, 6480000))
```

Outputs preserve **whole selected features**, without clipping their geometry.
Municipality selections use source municipality codes on entity layers and the
published municipality polygon for roads and geography. Explicit area and bbox
selections use geometric intersection. JSON attributes are written as JSON text.
Every extract gets a `.metadata.json` file recording its database publication,
selection, CRS and layer counts. All layers are read in one database snapshot;
large extracts stream through bounded batches. Existing outputs are refused.
Default output layers are buildings, component heights, properties, addresses,
EPC links, roads and boundaries. The full evidence and relationship tables stay
available through the `gis` views for SQL/Python queries.

## CLI

For repeated CLI use, put the non-secret connection settings above in a local
PostgreSQL service named `epsm_lake`. Supply the password using your normal
PostgreSQL password file or local environment, keeping it out of shell history.

```bash
export EPSM_DATABASE_URL='service=epsm_lake'
epsm-db extract uddevalla.gpkg --municipality 1485
epsm-db extract deso.gpkg --area-type deso --area-code 0114A0010 \
  --boundary-version 2025 --layers buildings component_heights
epsm-db extract study-area.gpkg --bbox 315000 6470000 325000 6480000
```

## Source coverage and derived boundaries

The supplied SCB archive contains 21 county and 290 municipality **overview**
polygons. Their edition is recorded as `source-260225`, using the delivered
artifact identifier rather than inventing a boundary year. The country outline
is their union. The 3,363 RegSO 2025 outlines are derived by dissolving the 6,160
DeSO 2025 polygons by their source `regsokod`; they are explicitly labelled as
derived, with source paths and the method in their attributes/provenance.

The supplied population grid has 115,118 cells, with original counts for total,
sex and age categories at `20251231`. Disclosure-protected totals and categories
are retained as published and are not forced to add up. The 165 cached median
income observations cover the municipalities supplied in the old snapshots,
not all of Sweden. Their observation year and RegSO edition stay separate.
Area-level statistics are context; no population or income is allocated to an
individual building.

## Operations on the database laptop

```bash
# Current stage and committed batch progress
/home/csd-lab/ubem/control/gis-lake/epsm-db status
cat /home/csd-lab/ubem/runs/gis-lake-2026-10-16/status.json
cat /home/csd-lab/ubem/runs/gis-lake-2026-10-16/progress.json
tail -n 20 /home/csd-lab/ubem/runs/gis-lake-2026-10-16/build.log
systemctl --user status epsm-lake-build.service --no-pager

# Pause / resume the replacement build
systemctl --user stop epsm-lake-build.service
systemctl --user start epsm-lake-build.service
```

The old `ubem-remote.service` is stopped and disabled. Its last status was saved
under this run directory, and its original database remains intact. Resuming it
would require `systemctl --user enable --now ubem-remote.service`; avoid running
both national builders concurrently. The replacement uses its own non-superuser
owner and a protected runtime credential file. It uses the already provisioned
geospatial Python runtime without changing the frozen legacy application.

Existing registered database readers inherit access through `ubem_readonly`.
The onboarding service still advertises the old default database until the main
lake switch is approved. No old database deletion or default switch is part of
this build.

## Additional SCB layers

See [Automatic SCB downloads](scb_open_data.md) for the ten population, housing,
social and energy table groups, GIS views, category filters and snapshot updates.

## Cadastral source polygons

`gis.property_parts` exposes all imported cadastral polygons, including source
features without a legal property-unit reference. Inspect
`property_reference_status` and `source_feature_type`. The normal
`gis.properties` layer aggregates polygons with an actual property-unit identity.
Unresolved parts are retained for GIS overlays and optional extracts.

## Model workflow

The version-0.4.0 stock workflow is deployed. See [the stock API](stock_api.md) for
lazy selections, catalogue cells, portable evidence, conversion and complete
classification-selected extracts. The existing `Lake` API remains the GIS and
administration interface.
