# Portable sources — v0.0.4

All declared national inputs, original archives, historical SCB snapshots, preparation
receipts and referenced height/event evidence live in one bundle. The initial root is
`data/raw/national-2026-10-13/` beneath this repository; `data/raw/` is excluded from Git.

Resolution precedence is `--source-root`, then `UBEM_SOURCE_ROOT`, then the repository
bundle. Profiles label provenance; they do not provide neighboring-repository fallbacks.
A relocated bundle works by changing the source root alone. Source bytes and original
manifests remain unchanged; verified path translations are separate bundle metadata.

<!-- SOURCE-INVENTORY:START -->
| Input | Local path |
|---|---|
| `lm_building_geometry_primary` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/buildings/byggnad_riks.gpkg` |
| `lm_building_geometry_epc_reference` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/buildings/byggnad_riks.gpkg` |
| `lm_building_archive` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/buildings/byggnad.zip` |
| `lm_building_metadata` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/buildings/byggnad_metadata.xml` |
| `lm_building_xml_root` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/buildings/xml` |
| `lm_address_archive` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/addresses/belagenhetsadress.zip` |
| `lm_address_metadata` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/addresses/belagenhetsadress_metadata.xml` |
| `lm_address_xml_root` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/addresses/xml` |
| `building_registry_locator` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/prepared/register/registry.sqlite` |
| `lm_property_geometry` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/lantmateriet/properties/fastighet_riks.gpkg` |
| `scb_municipality_boundaries_primary` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/scb/boundaries/shape_svenska_260225.zip` |
| `scb_municipality_boundaries_epc_reference` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/scb/boundaries/shape_svenska_260225.zip` |
| `scb_population_grid_2025` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/scb/population/befolkning_1km_2025.gpkg` |
| `height_staging_catalogue (unpublished staging)` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/prepared/heights/component-stage/handoff/component-height-catalogue.parquet` |
| `height_staging_reconciliation (unpublished staging)` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/prepared/heights/component-stage/handoff/reconciliation.json` |
| `height_path_mapping (unpublished staging)` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/prepared/heights/transfer-receipts/path-mapping.json` |
| `tower_component_register_root (unpublished staging)` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/prepared/components` |
| `epc_subjects (comparison only)` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/comparison/epc_subjects.parquet` |
| `epc_raw_root` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc` |
| `epc_before_datamodel` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/fore_v260/Datamodell.docx` |
| `epc_before_mapping` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/fore_v260/Mappningsfil_fore_v260.xlsx` |
| `epc_before_export_1` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/fore_v260/data/std_uttag_fore_260_del_1.txt` |
| `epc_before_export_2` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/fore_v260/data/std_uttag_fore_260_del_2.txt` |
| `epc_after_datamodel` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/Datamodell.docx` |
| `epc_after_form_specification` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/Formul„r version 2.8.pdf` |
| `epc_after_mapping` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/Mappningsfil_efter_v260.xlsx` |
| `epc_after_exports` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data` |
| `epc_after_export_2015` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2015.txt` |
| `epc_after_export_2016` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2016.txt` |
| `epc_after_export_2017` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2017.txt` |
| `epc_after_export_2018` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2018.txt` |
| `epc_after_export_2019` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2019.txt` |
| `epc_after_export_2020` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2020.txt` |
| `epc_after_export_2021` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2021.txt` |
| `epc_after_export_2022` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2022.txt` |
| `epc_after_export_2023` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2023.txt` |
| `epc_after_export_2024` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2024.txt` |
| `epc_after_export_2025` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/efter_v260/data/std_uttag_efter_260_2025.txt` |
| `epc_linkage_2_oracle (comparison only)` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/comparison/releases/2.0.0` |
| `epc_linkage_3_oracle (comparison only)` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/boverket/epc/comparison/releases/3.0.0` |
| `scb_snapshot_pinned (Göteborg only; not Uddevalla)` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/scb/snapshots/3cc70456ceba91447b3a143bf8375b3561129c61b6aa3c4aa1d36cff13b47410` |
| `scb_snapshot_uddevalla` | `/Users/ssanjay/GitHub/sweden_building_combined/data/raw/national-2026-10-13/scb/snapshots/44b929adf9cbb754ecb2b22253a383118bf01af92967ab7e0cd890c3e387dada` |
<!-- SOURCE-INVENTORY:END -->

## Bundle layout and verification

LM geometry, archives, XML and metadata live under `lantmateriet/`; both EPC regimes,
exports and specifications under `boverket/epc/`; snapshots, population and original
boundary archives under `scb/`. Prepared component partitions/lineage, registry locator
and height facts/evidence live under `prepared/`. Site assertions/PDFs live under
`site-events/`. Inventory, aliases, relocation journal and discovery coverage live
under `manifests/`.

Physical duplicates use bundle-internal hard links while preserving logical filenames
and source occurrences. Old locations contain text relocation notes, not symlinks.
Discovery coverage explicitly records inaccessible directories and excluded cloud,
network and symlink locations. Same-name files with different bytes remain untouched.

```sh
.venv/bin/sweden-ubem source validate --verify --source-root /absolute/path/to/bundle
.venv/bin/sweden-ubem source inventory --source-root /absolute/path/to/bundle
```

Verification checks hashes, file/reference closure, component and lineage row counts,
and all 63,842 referenced measurement units. It reports existing height publication
blockers separately from structural source validity. Current staging has 76,998 missing
height records, 14,779 unresolved representations, and incomplete EPSM/Core preflight.
Verification never promotes this staging release to publication readiness.

## Original inspection and database-only use

Processing, ordinary exploration, decision explanations, reporting and exports use
PostgreSQL. Explicit original-record inspection verifies the registered asset SHA-256
and reads its native FID/member/row locator from the configured bundle:

```sh
.venv/bin/sweden-ubem source inspect --record-id 123 --database-url "$UBEM_DATABASE_URL" \
  --source-root /absolute/path/to/bundle
```

Discover registered IDs through `mart.source_record_locators`. GeoPackage FIDs are
actual feature IDs; XML/EPC member rows are one-based; Parquet positions are zero-based.
Returned records come from original files. Missing, altered or ambiguous sources fail;
normalized database values are never presented as reconstructed originals.

Completed Uddevalla remains in Docker `sweden_ubem` on 55432. Its self-contained retained
artifacts and restore verification live in `output/retained/uddevalla-2026-10-12/`.
Historical manifests retain their original paths; new locations are recorded separately.
Comparison oracles and historical SCB snapshots remain distinct from production inputs.

Refresh the inventory appendix with `.venv/bin/python -m scripts.sync_manual_inventory`.
