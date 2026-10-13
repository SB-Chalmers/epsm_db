# National restart results — 2026-10-13

**Current state: partial database cleared at the user's request on 2026-10-13 at
16:30 Stockholm time.** The national importer and supervisor are stopped, and
automatic resume is disabled. Only `sweden_ubem_v004` on port 55433 was dropped;
Uddevalla, the source bundle and benchmark receipts are preserved. The partial
database had retained 3,776,512 component source rows and used approximately
2.75 GiB before removal. The 98.4 GiB (approximately 106 GB) full-run
figure is a conservative estimate including headroom, not an observed final size.
An explicit user request is required before resuming.

The completed Uddevalla database remains on Docker port 55432 with 45,300
buildings and 47,287 components. Its retained dump passed restoration and exact
comparison of all 29 public projections, including full geometry. The notebook
launcher `sh scripts/start_uddevalla_notebook.sh` selects this database through a
read-only connection; it is independent of the national rebuild.

Preservation, retirement of the exact native `sweden_ubem_v003` target, source
consolidation, duplicate removal, and the declared artifact cleanup are complete.
The bundle contains 112,495 physical assets and retains 166,650 original file
occurrences in its journal. Full verification includes 63,842 height measurement
file units. A real different-root test passed while the old source root was
unavailable.

## Accepted benchmark

The paired disposable databases consumed the same 1,048,576-component cohort,
complete building UUID cohorts, original feature FIDs, and complete certificates.
The EPC portion contains all 1,883,795 rows of the 13 active national text exports.
Archived EPC Parquet snapshots remain in the bundle but are not loader inputs;
they are excluded from ingestion capacity scaling.

| Measurement | Previous loader | Replacement loader |
|---|---:|---:|
| Loading, including deferred index construction | 76.1 min | 63.4 min |
| Database size | 34.83 GiB | 21.08 GiB |
| WAL generated | 81.65 GiB | 54.51 GiB |
| Peak worker RSS | 482 MiB | 814 MiB |
| Temporary I/O | 245 MiB | 1,952 MiB |

Loading, storage, and WAL improved by approximately 17%, 40%, and 33% respectively.
Worker memory and temporary I/O increased and are included in the rollout budget.
These are measured sample results, not national runtime or storage guarantees.
The runs used fresh databases in sequence on the same host; OS cache state was
uncontrolled. Smaller paired runs establish recovery behavior and supporting
parity, not the national speed estimate.

Thirteen evidence groups matched exactly in the final paired run. The remaining
lineage group exposed a public view type error: `original_values.object_version`
was a JSON number rather than the historical string. The correction changes only
that view's cast. Loader source, physical tables and indexes are byte-identical
between the measured and corrected snapshots. Re-importing the full geometry
cohort into another disposable database produced the exact historical checksum
for all 1,048,655 lineage rows. Original benchmark receipts remain verbatim; the
full-cohort correction receipt supplements their correctness acceptance. All 14
groups therefore pass the accepted evidence contract.

The corrected frozen application digest is
`7e5eb3b9b94a9c2432fdce707d89b10c0e3895d2e9c8c3bb6a14e551f62291b5`.
The unchanged loader source-projection digest is
`fcb8ecfd96f9aa6f86db41b287c9a24aa5641448abd3a427edf001fcba980319`.
The corrected application archive SHA-256 is
`684cfaf5f4ea209b1bbb9ae9404e1b4d21c27aa2cc22c471bff7823d45c7facc`.

## Rollout and remaining gates

National source ingestion started on 2026-10-13 at 16:20 Stockholm time in the
fresh native `sweden_ubem_v004` database on port 55433. The measured capacity model
estimates 98.4 GiB of final database space after explicit headroom and processing
allowance. Its total requirement is 128.2 GiB, including transient storage and a
15 GiB free-space reserve; approximately 135 GiB was available at admission.
The importer enforces the reserve at batch boundaries.

Measured stage scaling gives planning bounds of approximately 6.2–12.5 hours for
national loading and finalization. These are capacity and work-planning bounds,
not an ETA; distributions, cache, recovery, and subsequent Uddevalla validation
can change elapsed time.

The supervisor next independently reconciles 9,055,408 buildings, 9,464,839
components and 9,465,504 lineage rows. It then reproduces Uddevalla, compares keyed
outputs and multiplicity, and runs reports, exports and the exploration notebook
with the raw bundle disconnected. These gates are still pending at rollout start.
National publication remains a separate gate, blocked by the existing height
coverage/representation and EPSM preflight conditions.

Machine-readable receipts and live progress are under
`output/restart-2026-10-13/`: `performance-acceptance.json`,
`benchmarks/lineage-correction.json`, `rollout-gates.json`,
`national-build/supervisor-status.json`, and `national-build/heartbeat.json`.
The benchmark archives and input checksum receipts preserve the exact tested
implementations and sample provenance.

## Remote transfer sizing

A stat-based inventory of the entire source bundle measures 56,051,413,289 bytes
(56.05 GB / 52.20 GiB) of unique physical files, including bundle metadata.
Application, configuration, scripts, tests, notebooks and documentation add
1,301,974 bytes. The complete Uddevalla retention set adds 511,413,685 bytes when
hard links are preserved. Total transfer payload is therefore approximately
56.56 GB / 52.68 GiB uncompressed. Compression has not been measured. Remote
dependencies and a PostgreSQL/PostGIS runtime are installed or downloaded on the
destination rather than transferring this Mac's virtual environment or binaries.

The sources plus the current source-import capacity budget and Uddevalla
acceptance material total 194,179,078,159 bytes (approximately 194 GB / 181 GiB).
This budget covers national source loading and Uddevalla validation. Full-national
derived outputs, all municipal selections, reports, exports and backups are
additional workloads whose final footprint is not yet measured. A remote
allocation of 500 GB free SSD space is a planning recommendation for that broader
workflow; it is not a measured storage requirement. An initial compute allocation
of 8 vCPUs and 32 GiB RAM is a planning recommendation based on the existing
single-worker benchmark and database cache needs, not a remote speed guarantee.

Existing input coverage is national for core building/property/address/EPC and
population data. RegSO/income snapshots currently cover Uddevalla only. The known
height coverage/representation and EPSM preflight gates still apply remotely.
Moving execution does not by itself supply these missing inputs or establish full
national publication acceptance.

The machine-readable sizing receipt is `output/remote-transfer/size-and-capacity.json`.
No remote target has been selected or uploaded to.
