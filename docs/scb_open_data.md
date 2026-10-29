# Automatically downloaded SCB statistics

`config/scb-open-data.toml` selects ten national SCB tables. The downloader uses
PxWebApi **v2**, without credentials, fetching the latest published year of each
table and all published categories. It selects explicit DeSO 2025 / RegSO 2025
codes and the available country, county and municipality totals. Older,
unsuffixed DeSO/RegSO editions are excluded from these current-area snapshots.

| GIS view | SCB table | Contents |
| --- | --- | --- |
| `gis.scb_population` | TAB6574 | Population by age and sex |
| `gis.scb_households` | TAB6568 | Household types |
| `gis.scb_housing_tenure` | TAB6638 | Dwellings by tenure |
| `gis.scb_income` | TAB6679 | Income quartiles, median, mean and population counts |
| `gis.scb_employment` | TAB6680 | Employment, unemployment and labour-force status by age/sex |
| `gis.scb_education` | TAB6534 | Education levels, ages 25–65 |
| `gis.scb_cars` | TAB6589 | Cars in traffic/off road, registered to residents |
| `gis.scb_energy_use` | TAB3654 | Final energy use by consumer category and fuel |
| `gis.scb_heat_production` | TAB3452 | District-heat production and fuel use |
| `gis.scb_electricity_production` | TAB3451 | Electricity production and fuel use |

The social, population and housing tables contain DeSO/RegSO observations, with
administrative totals where SCB provides them. Energy data is at country,
county and municipality levels. The year can differ between tables: consult
`inventory.json` or each row's `period`. SCB's current geography is edition 2025;
this is independent of the observation year. Administrative geometry is the
supplied overview boundary artifact, recorded explicitly as `source-260225`.

## Download and resume

```bash
epsm-db fetch-scb config/scb-open-data.toml /path/to/new-scb-snapshot
```

Use the same catalogue/output directory to resume interrupted requests. Use a
**new directory** for a later source release. Requests stay below SCB's published
cell/rate limits, retry temporary errors and retain each original response,
request, retrieval time, table metadata, variable definitions and units. A
mid-download source update or changed selection fails instead of mixing releases.
Normalized JSONL files are bounded by the request selection; their file
checkpoints provide normal lake import/resume behavior.

On this laptop, the snapshot is:
`/home/csd-lab/ubem/data/scb-open-2026-10-16/`.
Its `download-status.json` records downloaded tables. The lake build automatically
imports this snapshot in the `scb_open_tables` stage. The queue waits for the
ongoing geometry/link publication stage to finish before checkpointing the next
source-ingestion stage and activating the updated code. Queue state is recorded
in `/home/csd-lab/ubem/runs/gis-lake-2026-10-16/scb-queue.json`.

For another completed lake build, the equivalent manual steps are:

```bash
epsm-db ingest /path/to/new-scb-snapshot/manifest.toml
epsm-db link
epsm-db publish
# Grant any newly introduced GIS views to the existing reader group.
epsm-db grant ubem_readonly
```

The supplied DeSO, derived RegSO, country/county/municipality polygons and vector
population-grid cells provide the matching geography. These additions fetch
statistical tables and use those stored polygons. Geometry is not duplicated per
statistical category/year.

## QGIS and Python

The named GIS views expose stable numeric `fid`, typed `geom` in EPSG:3006,
`area_type`, `area_code`, `boundary_version`, `period`, `indicator`,
`indicator_label`, `value`, `unit`, `value_status`, `dimensions` (source category
codes) and `dimension_labels` (source text). `gis.statistics` also includes
unresolved observations; `gis.area_statistics` provides all spatial observations.
Registered readers have SELECT access to the GIS views.

Filter geography, indicator and category **before mapping**. A view contains
multiple observations per polygon: ages, sexes and categories are distinct rows.
For example, a QGIS PostgreSQL subset for total population in DeSO areas is:

```sql
area_type = 'deso'
AND dimensions->>'Alder' = 'totalt'
AND dimensions->>'Kon' = '1+2'
```

Python's `lake.features()` supports the named SCB views, including filters for
`area_type`, `boundary_version`, `indicator`, `period`, municipality and bbox.
`lake.extract(..., layers=['scb_population'])` and the CLI's `--layers` argument
can export these layers as GeoPackages. An extract retains all selected
observations and their category labels; JSON columns become JSON text.

SCB null values and original status codes are retained, with `source_status:CODE`
when a status accompanies a cell. Missing/suppressed values are never replaced
with zero. Units, source reference periods and price type are retained; income
amounts in thousands of SEK are not silently converted. Category totals, sex/age
subgroups and overlapping age bands must not be summed together. These remain
area-level context, with no allocation to individual buildings.

## Official sources

- [SCB current API documentation and limits](https://www.scb.se/vara-tjanster/oppna-data/pxwebapi/)
- [SCB API v2 specification](https://statistikdatabasen.scb.se/swagger/v2/swagger.json)
- [DeSO/RegSO statistical tables](https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/regionala-statistikomraden-regso/regso-tabellerna-i-statistikdatabasen---information-och-instruktioner/)
- [Municipal and regional energy statistics](https://www.scb.se/en0203)
