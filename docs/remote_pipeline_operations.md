# Precision 7540 remote national pipeline

The SSH target is `csd-database` (`csd-lab-Precision-7540`). The project root is
`/home/csd-lab/ubem`. The Mac's completed Uddevalla database remains on Docker
port 55432; its cleared native national database remains paused.

## Current delivery

The 15,453,199,209-byte archive is uploaded independently of the chat by the Mac
LaunchAgent `se.ubem.national-upload-20261013`. It verifies the local SHA-256,
uses GNU rsync with `--append-verify` into an unpublished incoming file, inhibits idle sleep,
and retries after 30, 120 and 300 seconds. The initial short-probe transfer
estimate is superseded by live progress; sustained upload speed has been lower.

The Mac requires GNU rsync (installed with `brew install rsync`). Set
`rsync_binary` in the upload configuration to its absolute path; the current
path is `/opt/homebrew/bin/rsync`. Apple openrsync performed a prolonged CPU
scan during resume, leaving the receiver idle until timeout. SSH keepalives
now detect broken connections, and failed progress queries retain the last
known byte count rather than recording zero.

Local progress and logs are in `output/remote-execution/upload-status.json` and
`output/remote-execution/rsync.log`. To pause, write `{"paused": true}` to
`output/remote-execution/upload-control.json`. To resume, set it to false and run:

```sh
launchctl kickstart gui/$(id -u)/se.ubem.national-upload-20261013
```

The checksummed execution kit `kit-v3` is delivered; earlier kits remain retained.
The new kit uses distinct temporary files for concurrent status updates, fixing
a race between the heartbeat and delivery wait loop. The archive, application
and database configuration fingerprints are unchanged. `ubem-remote.service` waits
for the archive's ready marker, verifies its size and SHA-256, checks Zstandard
integrity, extracts into a new staging directory, and verifies all 109,742 file
occurrences before publishing the release directory. The archive wraps the
payload in `national-execution-2026-10-13/`. Its manifest lists 109,741 files;
the total of 109,742 includes the manifest itself. Kit v3 corrects the extraction
root and counting contract. Conflicts stop progression.

## One administrator setup

Administrator setup completed on 14 October 2026. Docker/Compose access and
`Linger=yes` were verified. This deployment now has a user-service drop-in that
starts the pipeline with `sg docker`; its Python process has the Docker group.
No logout or reboot is needed for the current deployment.

The laptop requires a sudo password. Run this in your own terminal:

```sh
ssh -t csd-database 'bash /home/csd-lab/ubem/control/bootstrap-admin.sh'
```

Follow the script's logout/login or reboot instructions so the user service gains
Docker group membership. It installs Docker Engine and Python venv support and
enables user-service persistence. Credentials remain outside scripts and logs.

The runtime uses the repository's digest-pinned PostgreSQL 17/PostGIS 3.5 image,
bound only to `127.0.0.1:55433`. Database files live under `database/pg17`; the
generated owner password is in the mode-0600 file `runtime/postgres.env`.

## Execution and acceptance

The service proceeds through runtime setup, disposable-database tests, restoration
of `uddevalla_reference`, national import, independent count reconciliation,
Uddevalla processing/comparison, capacity measurement, and national processing.
Source loading uses one worker, 8,192-row/64 MiB batches, and a 50 GiB disk reserve.

Uddevalla uses assessment date 2026-10-12 and the retained full-geometry baseline.
Only the existing keyed correction rules are accepted. Unexplained differences
stop the service. Ordinary consumers run while the raw folder is disconnected;
source inspection is separately tested for missing and changed originals.

After baseline acceptance, the service acquires the missing SCB 2025 RegSO/2024
income snapshots for the pinned 290-municipality roster and attempts bounded
component-height repairs using the frozen, fingerprinted height tools. Historical
files remain unchanged; enrichment uses a separate source workspace and database.

Height provider gaps, unresolved representations, unsupported geometry/roof cases,
and failed Core preflight remain blockers. They do not produce successful receipts.
Publication requires the enriched source and Uddevalla gates, national processing,
a final checksummed dump, and matching public projections after restoring that dump
into an owned disposable database. Energy simulations are outside this run.

## Status, pause and recovery

```sh
ssh csd-database 'python3 /home/csd-lab/ubem/control/kit-v3/remote_pipeline.py status --config /home/csd-lab/ubem/control/kit-v3/config.json'
ssh csd-database 'systemctl --user status ubem-remote.service --no-pager'
ssh csd-database 'journalctl --user -u ubem-remote.service -n 30 --no-pager'
```

Stage receipts, database progress, logs, comparisons and reports are under
`runs/national-execution-2026-10-13`. The service uses an exclusive lock. Permanent
gate failures exit 78 and prevent automatic restart. Check the failed stage and
its log before resuming. Source files, accepted releases and checkpoints are kept.

```sh
ssh csd-database 'python3 /home/csd-lab/ubem/control/kit-v3/remote_pipeline.py pause --config /home/csd-lab/ubem/control/kit-v3/config.json'
ssh csd-database 'python3 /home/csd-lab/ubem/control/kit-v3/remote_pipeline.py resume --config /home/csd-lab/ubem/control/kit-v3/config.json'
```

Resume reuses passed stage receipts and committed record positions. A changed
configuration, source bundle, field mapping or source parser requires inspection;
the supervisor does not silently adopt it. Interrupted extraction leaves a staging
directory; the next attempt uses a fresh directory and never overwrites it.

After acceptance, the executed exploration notebook is in the reported Uddevalla
acceptance directory. For remote database exploration, open an SSH tunnel:

```sh
ssh -N -L 55434:127.0.0.1:55433 csd-database
```

Connect through local port 55434 using the private runtime credential and
`sweden_ubem_v004_baseline`, or the database named in `active-release.json` after
publication. Use read-only sessions for exploration. Final verification receipts,
backup hashes and the active release pointer stay on the laptop; large database
dumps are not automatically copied back to the Mac.

## Implementation verification

The matching frozen validation suite has 70 passing synthetic checks and 30 passing
PostGIS checks. The two packaging failures found during verification (notebook
fixture location and backup runtime selection) were corrected and both retested.
The source-path, supervisor and height-contract checks passed. Actual remote import,
Uddevalla parity and publication still require their recorded execution gates.

## Reviewed kit identity (15 October 2026)

A read-only SHA-256 check of the deployed `kit-v3/remote_pipeline.py` matched
the reviewed source `scripts/remote_pipeline.py` exactly:
`919403b89b1e34c34aa6147816f7b6f9161ee87ac611c5d8cfb5843336f32d20`.
This identifies the delivery implementation, not the application bundle or a
completed national acceptance. No service was restarted for this check.
The nested-root, manifest-count and rejection tests run offline; the deployed
script remains frozen until an explicit operational update.
