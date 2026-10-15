# Release 0.3.0 — 15 October 2026

Stock and exploration catalogue inspection use the public Data Catalogue APIs, retaining classifications, incomplete drafts and source evidence. Physical Building assembly stays in EPSM.

The catalogue extra requires Data 0.6.0 or later; the EPSM extra requires Core 0.4.0 and Data 0.6.0 while retaining the compatible EPSM 0.2.0 minimum. See [the catalogue consumer guide](docs/catalogue_inspection.md).

This package version does not change the physical schema baseline, evidence contract or dataset release identity. No live database migration, deployment change or national ingestion is part of the release.

# Release 0.2.0 — 15 October 2026

Record retrieval, stock conversion, ingestion, processing, validation, exports and throttled progress. The sweden_ubem logger remains supported; database schema stays 0.0.4.

Libraries emit through Python's standard logging without configuring output.
Instrumented operations accept optional keyword-only `log_context`; identifiers
stay separate from scientific inputs, saved models and cache keys. INFO shows
major stages; DEBUG adds decisions and timing. Standalone commands support
`--log-level`, including `OFF`, where the package provides a command.

The EPSM application configures the combined stderr and optional DEBUG JSONL
output with `configure_logging`; `enabled=False` disables both destinations.
Existing callers and supported older collaborator signatures remain supported.
No new mandatory dependency is introduced by the logging implementation.

The coordinated release was requested for manual review and local validation
because GitHub refused to start several hosted jobs due to account billing.
Hosted checks that did not run are not counted as passes. See the
[coordinated verification record](https://github.com/SB-Chalmers/epsm_development/blob/main/docs/logging-verification.md)
for exact tested revisions, executed checks and remaining limitations.
