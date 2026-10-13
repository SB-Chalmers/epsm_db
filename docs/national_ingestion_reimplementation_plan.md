# National ingestion reimplementation plan

Reimplement initialization as a resumable import of shared national evidence, followed by database-only processing of municipal or national selections. Preserve Uddevalla (`1485`) as the functional acceptance target. Keep the current building, component, property, EPC, height, and context concepts; change their storage ownership and orchestration precisely.

Implementation is in progress in a separate replacement database; the original working database is preserved. Real national import and Uddevalla acceptance must finish before claiming completion. The first implementation supports one immutable national source snapshot per database and multiple processing releases over that snapshot. Importing multiple national source snapshots into the same database, deduplicating across historical snapshots, and automatic source updates remain separate work.

## Decisions

1. `db init` creates the schema, imports the declared national source bundle in bounded batches, and validates source readiness. Schema creation remains a separately callable internal function for tests.
2. External source files are read only by source import. Municipal processing, reporting, exploration, and exports use PostgreSQL. Optional site-event documents must also be imported before processing.
3. Source records are shared. Municipal and national datasets describe selections and publication state; they do not own copies of buildings, certificates, addresses, or source geometry.
4. Buildings own geometry, property, and area derivations. Complete certificates own EPC assessment work. Municipalities schedule those tasks and select their outputs.
5. Source completion, processing completion, and dataset publication are separate states. A successful import does not establish national context completeness, certificate linkage closure, or simulation readiness.
6. Keep the current PostGIS service, Python package, pure policy functions, public `mart` view names, and `UBEM(dataset="uddevalla")` behavior. Use PostgreSQL work tables and checkpoints rather than introducing an external queue or a permanent second copy of the sources in municipal Parquet files.

## Functional acceptance target

Capture the working database afresh before replacing it. The saved [12 October evaluation](../output/retained/uddevalla-2026-10-12/completed-build/evaluation/evaluation.json) and [EPC summary](../output/retained/uddevalla-2026-10-12/completed-build/epc/summary.json) provide these initial reference counts:

| Uddevalla projection | Reference count |
|---|---:|
| Primary building UUIDs | 45,300 |
| Components and matching height rows | 47,287 |
| EPC certificates | 3,922 |
| Parsed and assessed subjects | 4,063 |
| Matched subjects | 3,972 |
| Unresolved subjects with candidates | 85 |
| Subjects with no candidate | 6 |
| Accepted EPC relationships | 3,975 |
| Raw EPC rows | 9,788 |
| Fully matched certificates | 3,835 |
| Partly matched certificates | 4 |
| Unmatched certificates | 83 |

Counts are a first checkpoint. Acceptance compares keyed evidence and decisions, including multiplicity and nulls, rather than counts alone. All 4,063 subjects in the saved Uddevalla projection declare municipality `1485`; cross-boundary certificate behavior also requires synthetic fixtures.

Preserve these behaviors:

- Default browsing selects primary buildings with verified existing status. Supporting buildings, other statuses, and unknown statuses remain accessible through explicit filters.
- A purpose filter matches any recorded purpose. Repeated purposes, entrances, occurrence ordinals, and nullable main flags remain available.
- A selected UUID includes its complete component cohort. Preserve component IDs, normalized geometry hashes, original source FIDs, source version selection, and height ownership.
- Automatically union valid polygonal components, including overlaps and disconnected parts. Preserve the current exclusions for invalid geometry and municipality conflicts.
- Require both component ID and geometry hash for height association. Keep component heights and roof observations separate from building scalars and material assumptions.
- Union property intersections by property unit before calculating area. Preserve the distinction between spatial overlap and registered affiliation.
- Preserve `epc-identity-address-scope-v2`: exact property/house matching, guarded address fallback, ambiguity checks, construction-year conflicts, dates, reasons, and every parsed subject outcome.
- Keep EPC quantities on their reviewed certificate or calculation owner. Preserve date ties, unknown dates, mapping uncertainty, and temporal warnings.
- Preserve context overlaps, ranks, primary ambiguity, periods, dimensions, original protected values, and unit conversions. Supporting buildings remain outside the selected municipality's statistical context.
- Preserve read-only access, the exploration notebook, reports, saved scopes, and CSV, GeoParquet, and GeoPackage exports. Views can reconstruct provenance that no longer needs repeated storage.

The stale-context and preliminary exact-only certificate-expansion defects are explicit corrections. Any resulting Uddevalla row change must be listed with its cause. Additional nationally available candidates can also expose ambiguity; record those differences rather than weakening the comparison or silently asserting identical results.

## Minimal schema changes

The component identity already includes its geometry hash and is checked against
the building UUID and hash columns. Height records use the component primary key
foreign key plus a check that their input hash equals the component ID suffix.
This preserves both parent existence and hash equality without a redundant wide
unique index over `(dataset_id, component_id, geometry_hash)`.

Keep `dataset_id` on existing shared evidence tables. It will identify the national backing source snapshot there. Public views continue to expose the selected municipal dataset ID. This avoids renaming every source key or replacing established identities.

Extend `catalog.dataset` with:

- `dataset_kind`, constrained to `national_source`, `municipality_selection`, or `national_selection`.
- Nullable `municipality_code`, required to be a four-digit code only for municipality selections.
- `source_dataset_id`, referencing the backing national dataset for selections.
- `processing_release_id`, referencing the selected rule release.

Source rows have neither selection reference nor a municipality code. Selection rows require both references. Composite foreign keys enforce that the selected processing release uses the same backing source dataset. A catalog insertion check also verifies the parent dataset kinds. Preserve the existing municipal active-dataset mapping. Expose the national selection by an explicit name such as `sweden`; do not invent municipality `0000` or combine national and municipal active projections into a duplicate stock.

Add four tables:

| Table | Required identity and fields | Purpose |
|---|---|---|
| `catalog.processing_release` | `processing_release_id` PK; `source_dataset_id`; canonical input signature; policy versions; code and environment digests; field-mapping digest; assessment date; imported site-event source versions | Pins one immutable interpretation of shared evidence. Unique input signature prevents accidental duplicate configurations. |
| `scope.dataset_building` | PK `(dataset_id, building_id)`; `source_dataset_id`; `selection_role` | Stores selection membership and `primary` or `epc_support` role. Composite FKs bind membership to the selection and shared building. |
| `scope.dataset_certificate` | PK `(dataset_id, certificate_id)`; `source_dataset_id`; inclusion reason | Defines the complete reportable certificate cohort, including certificates without accepted links. |
| `catalog.work_unit` | Unique `(owner_dataset_id, processing_release_id, selection_dataset_id, stage, partition_key)` with `NULLS NOT DISTINCT`; input signature; state; checkpoint; counts; lease generation/owner; run reference | Supports bounded imports, dependency completion, retries, and task ownership. The owner is the backing source dataset; selection is nullable except for selection-specific tasks. |

Move `stock.building.selection_role` to `scope.dataset_building`. Add a building-level `municipality_values text[]` containing the union of current component assertions, with a GIN index. Preserve `municipality_code=NULL` when the assertions conflict. Municipal membership uses the assertions, so a conflicting UUID does not disappear merely because its canonical municipality is null. Add an indexed `processing_partition` for deterministic building-work ownership, assigned once from source components as described below; this is a scheduling key, not municipal or physical identity.

Add `processing_release_id` to the primary keys of five shared rule-dependent tables, while retaining their backing `dataset_id` and existing source foreign keys:

| Table | New primary key shape |
|---|---|
| `stock.component_relation` | backing dataset, processing release, left component, right component |
| `stock.building_footprint` | backing dataset, processing release, building |
| `evidence.building_property` | backing dataset, processing release, building, property unit |
| `evidence.epc_candidate` | backing dataset, processing release, building, subject, property unit |
| `evidence.epc_subject_result` | backing dataset, processing release, subject |

Each receives a composite FK to the processing-release/backing-dataset pair. Add nullable processing-release references to `catalog.process_run` and `quality.finding`; import findings belong to the backing snapshot, and derived findings belong to a pinned release. Add a nullable source-record locator to findings so municipal diagnostics can filter invalid EPC rows and unresolved addresses without exposing unrelated national findings. Validate derived run provenance against the same backing dataset and processing release.

Keep `context.building_area` selection-owned, preserving its current primary key and unique primary-area index. Add `source_dataset_id` so its source-area FK references shared `context.area`; change its building FK to `scope.dataset_building`, and add the composite selection/backing FK. Rank, primary assignment, and ambiguity are evaluated against the selection's pinned area cohort. Filtering nationally ranked assignments afterward would change boundary behavior. Expensive source geometries and statistics remain shared; the compact assignment rows are specific to the geographic selection. National context runs use the national area cohort. Context marts join shared areas and observations through `a.source_dataset_id=o.dataset_id` and area keys, replacing the existing joins that assume both rows have the municipal `dataset_id`.

Change the building FKs of existing `scope.membership` and `scope.coverage` to `scope.dataset_building`. Declare unique `(dataset_id, source_dataset_id)` on selections and unique `(processing_release_id, source_dataset_id)` on processing releases for composite references. Selection work also references the matching selection/backing/processing triple. These constraints prevent a local membership or derived result from referencing the wrong snapshot.

Keep existing keys on raw tables, components and lineage, register facts, purposes, entrances, addresses, property tables, EPC certificates/subjects/observations, height projections, and context source areas/observations. Pin their normalization rules and reviewed field mappings with the backing snapshot. In this first single-snapshot implementation, changing a normalized source mapping after sealing requires a fresh backing database. Changing only matching or context rules can reuse the existing source projection through a new processing release. Pin one canonical geometry vintage for each context area type/code; reject conflicting geometries under the retained area key. Multiple statistic periods can coexist through their observation identities.

Add narrow, indexed `certificate_id` and `subject_id` projections to both raw EPC tables; they are nullable for malformed identities. These allow complete certificate retrieval and reconstruction of subject provenance from the original rows. Preserve malformed source rows even when no typed identity can be assigned.

Keep `catalog.dataset_layer` as a summary, with an input signature and a stale state where applicable. A reusable summary requires matching completed work units; `loading_status='loaded'` alone is insufficient. Keep saved scope definitions and compute their membership through SQL. Initially retain `scope.coverage` as a compact, regenerable per-selection cache; measure it before replacing useful coverage with expensive repeated joins.

## Public projections and selection boundaries

Rebuild `mart` views through `catalog.dataset` and the membership tables. For example, building facts join the municipal membership to the shared building and to footprints/results for the selected processing release. Project `dataset_id='uddevalla'` and the membership role onto those shared facts. Source backing namespaces remain hidden from `mart.datasets`.

Certificate views start from `scope.dataset_certificate`. Include every subject, raw occurrence, observation, candidate, and subject outcome belonging to selected certificates, even if the certificate has no accepted building link. Make the building selection candidate-closed: include every persisted candidate building for those certificates, accepted or rejected, as supporting membership when it is outside the primary cohort. This allows stored certificate outcomes and displayed candidates to describe the same graph. Building views and exports use that membership; unrelated certificates of support buildings remain excluded.

Compute building-to-certificate counts and other local cardinalities from the selected certificate cohort only. Keep their `loaded_relations` label. Nationally complete relationship counts require separate reconciliation. Keep shared `evidence.epc_certificate.closure_status='unknown'` in the first implementation; any later verified closure must be a processing-release-specific projection, not a mutation of the shared source certificate.

Define each municipal selection from source evidence in this order:

1. Select primary UUIDs where the current component municipality assertions include the requested code; retain every component of those UUIDs.
2. Select certificates with any successfully parsed subject declaring that municipality. Keep all rows and subjects of those certificates. Do not discover further certificates through supporting buildings.
3. Assess those complete certificates against relevant national property/address/building evidence using the same policy used for final linkage.
4. Add every candidate building of the selected certificates as support unless it is already primary. Also retain relevant property/address ambiguity competitors needed to inspect the decision. Retrieve their complete component cohorts. This candidate-closed support policy fixes preliminary exact-only expansion and can produce additional support rows; enumerate those differences explicitly. Do not discover the support buildings' additional certificates.
5. Project municipal context from the selected municipality/RegSO source coverage. Outside supporting buildings remain `not_applicable` for local context, even if another job has prepared their global context.

For the selected certificate cohort, `mart.epc_candidates` includes its complete assessed candidate graph and `mart.epc_subject_results` includes its complete outcomes. Candidate-closed membership makes those views consistent with building relations. `mart.epc_certificates.loaded_building_count` and per-building certificate counts reduce only that selected cohort. Test accepted outside candidates, rejected outside candidates, and competing outside UUIDs before accepting these views.

Property and source-area interfaces must also retain their local scope. Use the current selected-footprint bounds for the property-source projection and the pinned municipality polygon for population selection. Shared national tables are an internal lookup resource, not authorization to show every national property or statistical area in a municipal report.

The national selection contains each backing building UUID once. Count municipality conflicts separately. Do not construct national stock by concatenating municipal projections containing repeated supporting or conflicting memberships.

## Source bundle and verification

Use the existing source inventory for location resolution. Produce a pinned bundle manifest containing logical source keys, exact contracts, logical file names, SHA-256, byte sizes, chosen source versions, coverage, parser/projection versions, CRS, and normalization/mapping digests. Absolute host paths are locations rather than identities.

Choose one tested Python/runtime dependency set for development and CI, lock the resolved dependencies, retain the pinned PostgreSQL/PostGIS image, and record GEOS, GDAL, PROJ, and database versions. Record import configuration and ownership layout with the bundle. Reproducibility checks compare semantic outputs across supported hosts; differing host locations or generated execution IDs do not define different source evidence.

Replace the current set-of-file-hashes source identity with the canonical ordered logical-file manifest. Preserve file multiplicity and name-to-content association. Generate raw-record IDs as a canonical hash of source-version identity, logical file name, member, kind, and original FID or record ordinal; avoid hashing the absolute filesystem path. Raw and source-version IDs will change from their existing contracts. Compare them through logical-file content/member/ordinal mappings rather than requiring the old generated hashes to remain identical.

Hash CSV/XML bytes during the complete import stream where practical. Seekable Parquet and GDAL readers require a genuinely immutable source snapshot or pre/post verification once per asset per national import. A cached size/mtime check does not provide equivalent integrity. On a successful completed import, municipal jobs reuse database evidence and perform no source hashing. Resume verifies that the source asset still belongs to the pinned release.

Retain the existing normalized component geometry hash and component identifier contract. Retain source archives in their current external locations. Create only temporary staging rows, with automatic removal after successful merge; persistent municipal file copies are unnecessary for the central database workflow.

## National source ingestion

Import independent sources separately. Read batches into temporary staging tables, then merge validated rows into shared tables and commit the associated checkpoint. Use deterministic keys and constraints so replay cannot duplicate rows. Keep incomplete source partitions invisible to public selections until reconciliation passes. PostgreSQL recommends `COPY` for bulk loading and `ANALYZE` after substantial loading; use those operations while retaining correctness checks. [PostgreSQL guidance](https://www.postgresql.org/docs/17/populate.html)

| Source | Bounded import and fidelity requirements |
|---|---|
| LM geometry and component register | Stream original GPKG features with FIDs; stream all 145 component Parquet partitions and lineage. Validate register manifest, counts, CRS, IDs, and geometry hashes. Create one shared building per canonical component UUID. Keep register-only or historical XML evidence in raw storage without inventing geometry-supported stock membership. |
| Building XML and locator | Import the pinned SQLite locator mapping into temporary staging. Parse XML records once, preserving file and ordinal, and verify the locator-selected record and UUID before applying register facts. Do not replace this choice with an unreviewed latest-record policy. |
| Address XML | Parse all occurrences with an all-record bounded iterator. Preserve CRS and axis handling. Select the same deterministic first occurrence in sorted logical-file/ordinal order used by the existing reader; retain other raw occurrences. |
| Properties | Stream GPKG features once, preserving FIDs, part/unit identities, labels, and source CRS. Build shared spatial and normalized designation indexes. |
| EPC exports | Stream both regimes and every original row. Populate typed identity projections without discarding malformed rows. Normalize subjects and observations over complete certificate groups, using SQL aggregation or bounded certificate jobs rather than one national Python dictionary. Preserve every flattened source locator and conflicting value. |
| Height catalogue and measurements | Import the catalogue first. Query distinct referenced assets from PostgreSQL and stream each once. Match absolute row offsets or component IDs, verify UUID and input geometry hash, and store each original measurement once. Remove the unbounded whole-file measurement cache. |
| SCB population | Import the national grid once, preserving original periods, fields, dimensions, and protected values. |
| RegSO and income | Import each declared saved snapshot once and record municipality/year coverage. The available Uddevalla snapshot does not establish all-Sweden coverage. Import a separately acquired national release or additional saved snapshots when available. |
| Site-event evidence | Import the optional event JSON and referenced document hashes before processing; record site scope and date basis. Processing reads the imported assertions only. |

The canonical register currently declares 9,055,408 UUIDs, 9,464,839 components, and 9,465,504 lineage/source rows. Reconcile those separately; component and original-feature counts differ legitimately. The height catalogue also has 9,464,839 rows, but its reconciliation reports 76,998 missing height records and publication blockers. Preserve that staging status and unresolved measurement evidence. Seal the backing snapshot after its declared imports and reconciliations complete. Additional undeclared source snapshots require a fresh backing database in this first implementation.

A source import may contain geographically partial context. Its metadata must state which municipalities and periods are covered. Dataset planning reads that coverage internally and distinguishes unavailable inputs from a loaded lookup with no matching record. Acquisition remains a separate operation; initialization does not perform uncontrolled WFS/PxWeb requests.

## Processing dependencies and bounded work

Use these dependencies explicitly:

- Building footprints and component relations depend on complete canonical component cohorts and the geometry policy.
- Register and height facts depend on their corresponding verified source projections; height association also depends on matching component hashes.
- Property links depend on resolved footprints and property evidence.
- Municipality, RegSO, and population assignments are separate internal stages, each depending on footprint geometry and its own area release.
- Income coverage depends on the selected RegSO assignment and actual income observations, not merely the presence of a RegSO link.
- EPC assessment depends on geometry, register, addresses, properties, property links, complete certificate/subject raw rows, field mappings, assessment date, and optional imported site events.
- Coverage, saved scopes, and publication depend on completed required derivations and current dependency signatures.

Preserve the existing 13 public layer names as summaries. Internally split context work by area type, so importing population or RegSO later cannot leave the old generic context stage falsely complete.

Assign building work partitions once during source projection: use the smallest non-null source component owner-tile value as a routing group, sort complete UUIDs within it, and divide the group into deterministic bounded chunks. Use a declared fallback group for missing tile values. Pin this layout with the source bundle and store each building's partition key. A municipal request schedules the intersecting global partitions; each job owns its entire recorded partition, so requests cannot create overlapping batches with different work keys. Extra nearby buildings may be prepared internally without adding them to the municipal selection.

Geometry/property tasks use those global building partitions. EPC work owns individual regime-qualified complete certificate IDs, with workers claiming batches of tasks to reduce scheduling overhead. Context, membership, coverage, and scope tasks additionally identify their selection because their results depend on its area or certificate cohort. The work-unit owner remains the backing dataset in all cases; selection-specific tasks use `selection_dataset_id`. Workers claim tasks with database locks and lease generations.

Enforce that global geometry/property/EPC tasks have `selection_dataset_id IS NULL`, and source import tasks also have `processing_release_id IS NULL`. Selection-specific tasks bind their selection, source, and processing release through composite FKs. Execution batches may subdivide a recorded building partition with internal checkpoints; they may not change its ownership or silently repartition completed work.

Geometry/property/context batches contain complete UUID cohorts, never truncated pieces. EPC tasks retrieve complete certificate subjects and all relevant property/address candidates, including competing buildings and other subjects needed for shared-address checks. A fixed building batch alone is not a sufficient EPC evaluation scope. Shared-address context must include other relevant source subjects even when their certificates are not being scheduled; only the owned certificate's results are committed.

Generate spatial and identity candidate sets with indexed SQL. Keep the reviewed Python policy functions initially; supply complete inputs from database queries and verify their results against the baseline. Implement certificate/subject/building cardinality reductions in SQL. Exceptionally large certificates or properties require streamed candidate generation and database reductions; do not allow one unusual group to defeat the memory limit.

Use server-side cursors or keyset pagination for large query results. A normal Psycopg client cursor can buffer an entire result even when Python subsequently iterates it. [Psycopg cursor documentation](https://www.psycopg.org/psycopg3/docs/advanced/cursors.html)

Start benchmarks with 8,192 source rows per batch, approximately 2,000 complete UUIDs per geometry task, and one worker. These are configurable starting values, not guarantees; limit bytes as well as row counts because geometry and raw XML/EPC payload sizes vary. Increase concurrency only after measuring worker RSS, database memory, WAL, and temporary-file growth.

## Resume and publication

Work-unit states are pending, running, complete, failed, or stale. Record the source asset and reader position for imports; record owned UUID/certificate partitions for derivation. Commit output rows and the checkpoint together. Enforce a fencing generation when claiming and committing work, with database locks around reassignment. A stalled worker cannot later commit into a work unit reclaimed by another worker. Test this independently of ordinary interrupted-run recovery.

Parquet checkpoints use verified row groups/ranges. GPKG checkpoints use a stable FID strategy validated against the source. XML/CSV recovery can re-stream the current file to the committed ordinal when a safe seek boundary is unavailable. Completed files and completed municipal processing are not reread from source.

Pin the full dependency signature before a task starts. Reuse a completed task only if its signature matches. Before source sealing, completing a previously declared unfinished source job invalidates unpublished dependent work. After sealing, new sources or normalized mapping changes require a fresh backing database. Different matching/context policies, assessment dates, or selections of already imported site events create a new processing release. Published dataset selections remain immutable and continue to reference their previous release.

A selection is activated only after its required tasks and validation pass. Activation updates its public reference atomically. Whole-country publication additionally reconciles unique UUIDs, municipal coverage/conflicts, required task coverage, and source availability. National raw import alone cannot set EPC closure to complete.

## Storage and query efficiency

Share source and normalized entity rows across selections. Keep compact membership IDs and compact decision records instead of repeated municipal evidence copies. Preserve genuinely different source and derived geometry, such as original LM outlines and the building union.

Remove copied source-locator arrays and source attributes from EPC decision JSON where they can be reconstructed through `subject_id`, indexed raw rows, and source metadata. Reconstruct the existing public detail fields in views/reports. Replace observation locator arrays with indexed queries over contributing certificate rows, preserving the original nonempty-field contribution rule. Keep the raw-value sets, conflict statuses, decisions, rejection reasons, and all original source rows.

Avoid combining this rewrite with a blanket removal of every child `original_values` column or replacement of public UUID/component keys with surrogate identities. Those changes need separate measurements and equivalence checks. Existing child payloads can remain in the first shared source projection while the largest redundant provenance arrays are removed.

Retain B-tree indexes for building ownership, certificate/subject lookups, and work ownership; GiST indexes for source/derived geometry; normalized property/address match indexes; and the municipality-assertion lookup. Index derived results by backing dataset and processing release. Run `ANALYZE` before expensive joins. Physical table partitioning is optional after profiling; logical task partitions are required immediately.

Push `pick()`, area-type filters, purpose filters, and latest-certificate selection into SQL. Stream reports and exports instead of holding all output tables or the expanded building-context observation join in memory. Keep the existing output formats and relationship keys.

## Proposed commands

These illustrate the intended interface; they are not commands supported by the current implementation.

```sh
sweden-ubem db init --source-bundle config/national-source-bundle.toml --resume
sweden-ubem dataset plan --dataset uddevalla --municipality 1485
sweden-ubem dataset process --dataset uddevalla --municipality 1485 \
  --assessment-date 2026-10-12 --site-event-source uddevalla-site-events
sweden-ubem dataset validate --dataset uddevalla
sweden-ubem dataset report-epc --dataset uddevalla --output output/validation/new-epc-report
sweden-ubem dataset process --dataset sweden --national \
  --assessment-date 2026-10-12 --site-event-source uddevalla-site-events
```

The bundle includes the existing height mapping, SCB snapshot, reviewed EPC mapping documents, and optional imported Uddevalla site evidence. `--site-event-source` resolves an already imported source version. An omitted assessment date is pinned once when the processing release is created; the reproducibility path always supplies it explicitly. Repeating initialization on a completed identical bundle is a no-op. A different bundle is rejected in this first single-snapshot implementation until an explicit fresh rebuild is requested.

Retain the backed-up, target-identified development rebuild procedure. Ordinary initialization does not destroy existing data. A source-only initialization does not automatically publish a municipal or national model.

## Implementation sequence

| Step | Concrete changes | Gate before proceeding |
|---|---|---|
| 1 Capture baseline | Dump the working database and verify restore into a separate database. Capture all Uddevalla mart projections, source manifests, mappings, dates/events, saved scopes, and representative exports/notebook results. Add failing regressions for stale context and guarded-address support. | Restorable backup and a keyed comparison fixture exist. |
| 2 Introduce identities | Create a clean schema baseline with the four tables and specified alterations. Rebuild membership-aware mart views and database roles. Update direct SQL consumers in scripts/tests as well as API queries. | Synthetic views preserve grains, source FKs, roles, and certificate cohorts. |
| 3 Build source import | Add bounded readers, COPY/merge helpers, portable record identity, work checkpoints, and source reconciliation. Cover geometry/register/address/property/EPC before height/context refinements. | Repeated imports and interrupted imports yield identical source rows without duplication. |
| 4 Complete evidence import | Add grouped height measurement import, available SCB snapshots, optional events, and source coverage metadata. | No false promotion of missing/unpublished or geographically partial evidence. |
| 5 Build database processing | Replace external selection/expansion with SQL queries, complete building/certificate tasks, dependency signatures, and atomic selection publication. | Process with source readers disabled; reversed/concurrent municipality order gives identical semantic results. |
| 6 Reduce duplicated payloads | Reconstruct EPC provenance/detail fields through joins; add SQL filtering and streamed report/export paths. | Public evidence remains reconstructible and exports/reports pass parity. |
| 7 Validate fresh national import and Uddevalla | Initialize a separate fresh development database, then process Uddevalla using the pinned baseline date/events. Compare every public projection and execute the notebook/exports. | Zero unexplained semantic differences; known corrections have explicit keyed delta reports. |
| 8 Benchmark and publish | Measure the full national import, large urban and extensive rural processing, cross-boundary certificates, retries, and concurrent jobs. Validate national membership and coverage before national publication. | Measured storage/memory/runtime fit the selected host, and recovery/publication tests pass. |

Refactor the existing 1,873-line `dataset.py` into source import, task execution, and selection projection modules, leaving it as a thin facade where useful. Suggested modules are `ingest.py`, `jobs.py`, and `selection.py`. Keep `components.py`, `linkage.py`, and `epc_assessment.py` as the policy reference during the first implementation. Update `database.py`, `cli.py`, `explore.py`, `exports.py`, `epc_reporting.py`, `comparison.py`, and the evaluation scripts wherever their direct SQL assumes municipal ownership of source rows.

## Acceptance checks

Require all existing tests plus these additional gates:

1. Compare public rows by business key using full joins or `EXCEPT ALL`, preserving duplicates and nulls. Require exact identity, status, owner, mapping, method, reason, date, membership, rank, and primary-decision equality where no intentional correction applies.
2. Preserve exact normalized component geometry hashes. Compare derived union geometry using `ST_Equals`, so vertex/ring ordering is not mistaken for changed geometry. Predeclare narrow numeric tolerances only where equivalent spatial computations differ: area `max(1e-6 m2, 1e-9 * area)` and overlap fraction `1e-9` are initial candidates. Tolerances may not conceal a changed 50% acceptance decision, rank, or boundary ambiguity.
3. Exclude generated run/finding IDs, raw/source-version IDs changed by the portable identity contract, timestamps, schema-version labels, physical paths, and export ordering from semantic equality. Verify the old/new logical-file content/member/ordinal mapping and equivalent rules. Building UUIDs, component IDs/hashes, and established certificate/subject identities remain unchanged.
4. Preserve the synthetic cross-boundary certificate contract: selected certificate 10 is complete, its supporting building is available, and unrelated certificate 99 does not enter the municipal cohort recursively.
5. Process municipalities in both orders, concurrently, and through a national request. Verify unchanged previously published municipal results, one shared copy of evidence, and consistent global versus selected counts.
6. Before sealing, derive context while a declared context import remains unfinished, then finish that import. Validation must identify stale dependencies, and a new publication must equal a complete single run. After sealing, reject undeclared context additions. Include municipal boundary cases where national ranking would differ from ranking the selected area cohort.
7. Test exact and guarded-address support discovery against complete national candidates, including competing UUIDs, shared EPC addresses, invalid geometry, missing register facts, and conflicting municipality assertions.
8. Make source files, bundle directories, SCB snapshot paths, height measurements, and optional event documents unavailable after import. Dataset planning, new processing releases over imported evidence, resumed derivation, exploration, provenance reconstruction, reports, and exports must still succeed. No-op processing must perform zero external source parsing or hashing.
9. Interrupt imports and processing between batches. Verify resume, idempotence, lease fencing, final equality, and absence of partially published layers. Attempt a late commit from a reclaimed worker and require rejection.
10. Change a policy, imported event selection, or assessment date and verify new release behavior. Reject normalized field-mapping changes on a sealed source snapshot; exercise them through a fresh backing database. Preserve previous published outputs.
11. Validate CSV, GeoParquet, and GeoPackage relationship keys and complete selected certificates, execute the notebook, and exercise the read-only role.
12. Reconcile national source counts to imported, excluded, invalid, and unresolved records. Successful job states cannot substitute for this independent reconciliation.

## Capacity and completion criteria

At planning time the working Uddevalla database occupies 1,092,709,523 bytes and the workspace volume has approximately 159 GB free. The previous proportional national estimate is about 218 GB before WAL, temporary operations, backups, and development headroom; it is not a prediction of the optimized schema. A full national rebuild needs a storage assessment of the actual PostgreSQL/Docker volume or chosen server before execution. Estimate height-file inventory and source projection expansion as well as building rows; many referenced small measurement files may dominate initialization time.

Measure source import and processing separately: external bytes read, rows per second, wall time, worker peak RSS, database heap/TOAST/index sizes, WAL, temporary space, query plans, and interrupted-run recovery. Memory must remain bounded by configured working batches and workers as processed volume increases. EPC groups need an explicit spill/stream strategy. Establish runtime and resource targets from the first measured benchmark rather than promising an untested speedup.

The first implementation is complete when a fresh database imports the declared national evidence once, reproduces the intended Uddevalla functions through database-only processing, reports every intentional difference, and demonstrates bounded execution and restartability. National model publication additionally requires national derivation and source-coverage validation. Successful evidence ingestion does not claim UBEM simulation readiness.
