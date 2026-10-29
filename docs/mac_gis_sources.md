# Add the Mac's DeSO and NVDB sources

The incoming directory has been prepared on the database laptop:
`/home/csd-lab/ubem/data/gis-lake-incoming/`.
All three files arrived on 16 October 2026 and their source metadata was inspected:

| File | Layer | Features | CRS |
| --- | --- | ---: | --- |
| `DeSO_2025.gpkg` | `DeSO_2025` | 6,160 | EPSG:3006 |
| `nvdb/Sverige_Vägbredd_GeoPackage.zip` | `NVDB_DK_O_31_Vagbredd` | 923,322 | EPSG:3006 |
| `nvdb/Sverige_Vägtrafiknät_GeoPackage.zip` | `NVDB_DK_O_88_Vagtrafiknat` | 2,498,565 | EPSG:3006 |

DeSO has been imported into `epsm_lake` and published as `gis.deso`.
NVDB ingestion is queued in the active replacement build. Registered-reader
Python access, GIS geometry metadata and an actual GeoPackage extract have been
checked operationally. Full national ingestion and validation remain in progress.

## Transfer from the Mac

With Tailscale connected, run these in the **Mac's Terminal**:

```bash
rsync -av --progress --partial \
  '/Users/ssanjay/Library/CloudStorage/OneDrive-Chalmers/Desktop/postdoc/Data/DeSO_2025.gpkg' \
  'csd-lab@csd-lab-precision-7540.tailb64966.ts.net:/home/csd-lab/ubem/data/gis-lake-incoming/'

rsync -av --progress --partial \
  '/Users/ssanjay/Library/CloudStorage/OneDrive-Chalmers/Desktop/postdoc/nvdb/' \
  'csd-lab@csd-lab-precision-7540.tailb64966.ts.net:/home/csd-lab/ubem/data/gis-lake-incoming/nvdb/'
```

This copies the NVDB directory, including its GeoPackage ZIP archives, and retains the Mac
originals. SSH uses the laptop's Linux account `csd-lab`; this authentication is
separate from participant PostgreSQL credentials. The laptop's SSH service was
active when this guide was prepared.

## Import into the new lake

`config/gis-context.toml` declares:

- **DeSO 2025:** area type `deso`, boundary edition `2025`, typed polygon geometry.
- **NVDB:** separate source declarations for the delivered road-width and road-network
  archives, with their exact layer names. Source FIDs, network identifiers,
  original attributes and geometries are preserved.

On the database laptop, use the new branch/environment and set
`EPSM_DATABASE_URL` to a **fresh GIS lake database**. The currently running schema
0.0.4 service is a separate deployment. The manifest now uses the inspected layer names and explicit field mappings:

- DeSO: `desokod` is the area code, `kommunkod` the municipality and `regsokod`
  the parent RegSO code. The source revision is `2025_v1`, with boundary edition
  `2025`. All 6,160 DeSO codes are distinct. Parent references cover 3,363 RegSO
  codes, across 290 municipalities and 21 counties; matching RegSO polygons have
  not been supplied. `kommunnamn` is retained as a municipality attribute, not
  used as the name of each DeSO area.
- Both NVDB themes: `ELEMENT_ID` maps to the network reference, while
  `START_MEASURE` and `END_MEASURE` retain the source segment measures.
  `ELEMENT_ID` alone is not used as a feature key. Road widths (`Bredd`), network
  categories (`Nattyp`), validity dates and source extent lengths remain in
  `attributes`.
- The NVDB GeoPackages declare a generic geometry type. The importer preserves
  actual feature geometries and the GIS views select line, point and polygon
  features by geometry type; layer metadata alone does not establish that every
  feature has the same type.

```bash
cd /home/csd-lab/epsm_db
epsm-db init
epsm-db ingest config/gis-context.toml \
  --source-root /home/csd-lab/ubem/data/gis-lake-incoming \
  --kinds areas roads
epsm-db link
epsm-db publish
```

Every spatial layer must use EPSG:3006. A mismatched CRS produces a source error
rather than being assigned the wrong CRS silently. Zipped GeoPackages are
extracted to temporary files in bounded chunks; allow space for their uncompressed
size. Source checkpoints refer to the original archive. Resume follows the same
sorted member/layer/feature order; Parquet and ordinary vector sources also have
streaming readers.

In QGIS, use **gis.deso** or **gis.boundaries** for DeSO and **gis.roads** for
NVDB line features. Road themes can be filtered using `source_layer`. The
`attributes` JSON column retains the original source columns. Point/polygon
assets have **gis.road_points** and **gis.road_areas** layers; nonspatial NVDB
records remain inspectable through **gis.road_attributes**. `ELEMENT_ID` is retained
as a network reference, with no assumption that it uniquely identifies a theme
feature or event.

The replacement build is running; see [GIS lake access](gis_lake_access.md) for
connection settings, extract commands, source coverage and monitoring. The legacy
national ingestion is paused and its database is retained.
