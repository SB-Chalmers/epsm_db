from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .hashing import sha256_file


class HeightReleaseBlocked(RuntimeError):
    """Raised when component heights have not passed their publication gates."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        reasons = "; ".join(str(item) for item in report["blockers"])
        super().__init__(f"Component-height release is not publishable: {reasons}")


REQUIRED_HEIGHT_COLUMNS = {
    "building_id",
    "component_id",
    "input_geometry_hash",
    "owner_tile_id",
    "geometry_status",
    "height_status",
    "representative_height_m",
    "eave_height_m",
    "ridge_height_m",
    "roof_form",
    "failure_reason",
    "processing_status",
    "processing_error",
    "evidence_record_path",
    "evidence_record_sha256",
}


def validate_height_staging(
    path: Path,
    *,
    expected_sha256: str | None = None,
    geometry_register: Path | None = None,
    evidence_path_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate a reconciled height catalogue without claiming publication readiness."""
    path = path.expanduser().resolve()
    catalogue = path / "component-height-catalogue.parquet" if path.is_dir() else path
    reconciliation_path = catalogue.parent / "reconciliation.json"
    blockers: list[str] = []
    if not catalogue.is_file():
        raise FileNotFoundError(catalogue)
    if not reconciliation_path.is_file():
        raise FileNotFoundError(reconciliation_path)
    reconciliation = json.loads(reconciliation_path.read_text())
    declared = (reconciliation.get("sha256") or {}).get(catalogue.name)
    actual = sha256_file(catalogue)
    if declared != actual:
        raise ValueError("Height staging catalogue differs from reconciliation SHA-256")
    if expected_sha256 and actual != expected_sha256:
        raise ValueError("Height staging catalogue differs from --expected-sha256")

    import pyarrow.parquet as pq

    metadata = pq.ParquetFile(catalogue).metadata
    columns = set(pq.read_schema(catalogue).names)
    missing_columns = sorted(REQUIRED_HEIGHT_COLUMNS - columns)
    if missing_columns:
        raise ValueError("Height staging catalogue lacks columns: " + ", ".join(missing_columns))
    expected_rows = int(reconciliation.get("component_rows") or 0)
    unique_rows = int(reconciliation.get("unique_component_ids") or 0)
    if metadata.num_rows != expected_rows or expected_rows != unique_rows:
        raise ValueError(
            "Height staging row counts do not reconcile with unique component identifiers"
        )

    register = Path(
        geometry_register or reconciliation.get("geometry_register_root") or ""
    ).expanduser()
    register_manifest = register / "manifest.json"
    lineage = register / "source-lineage.parquet"
    if not register_manifest.is_file() or not lineage.is_file():
        raise FileNotFoundError(
            f"Canonical geometry register requires manifest.json and source-lineage.parquet: {register}"
        )
    register_data = json.loads(register_manifest.read_text())
    if int(register_data.get("component_count") or 0) != expected_rows:
        raise ValueError("Height catalogue and canonical geometry register counts differ")
    status = str(reconciliation.get("status") or "unknown")
    selective_jobs = int((reconciliation.get("queue_counts") or {}).get("complete") or 0)
    awaiting_coverage = int(
        (reconciliation.get("queue_counts") or {}).get("waiting_source_coverage")
        or reconciliation.get("missing_height_records")
        or 0
    )
    exact_baseline = expected_rows - selective_jobs - awaiting_coverage
    if exact_baseline < 0:
        raise ValueError("Height staging provenance counts exceed the catalogue row count")
    blockers.extend(str(value) for value in reconciliation.get("blockers") or [])
    if status not in {"publication_blocked", "published"}:
        blockers.append(f"unexpected staging reconciliation status: {status}")
    if reconciliation.get("epsm_preflight_status") != "passed":
        blockers.append("EPSM/Core preflight has not passed")
    return {
        "path": str(catalogue.parent),
        "catalogue": str(catalogue),
        "catalogue_sha256": actual,
        "catalogue_byte_size": catalogue.stat().st_size,
        "reconciliation": str(reconciliation_path),
        "reconciliation_sha256": sha256_file(reconciliation_path),
        "geometry_register": str(register.resolve()),
        "geometry_register_manifest_sha256": sha256_file(register_manifest),
        "source_status": status,
        "publication_status": "candidate",
        "ubem_ready": False,
        "production_promotable": False,
        "component_rows": expected_rows,
        "unique_component_ids": unique_rows,
        "unique_building_uuids": int(reconciliation.get("unique_building_uuids") or 0),
        "exact_baseline_records": exact_baseline,
        "selective_component_jobs_completed": selective_jobs,
        "awaiting_coverage_components": awaiting_coverage,
        "missing_height_records": int(reconciliation.get("missing_height_records") or 0),
        "unresolved_representation_components": int(
            reconciliation.get("unresolved_representation_components") or 0
        ),
        "epsm_preflight": reconciliation.get("epsm_preflight_status"),
        "columns": sorted(columns),
        "evidence_path_map": evidence_path_map or {},
        "blockers": blockers,
        "reconciliation_data": reconciliation,
        "geometry_register_data": register_data,
    }


def validate_height_release(path: Path) -> dict[str, Any]:
    """Reject staging snapshots and validate the published component-height contract."""
    manifest_path = path / "manifest.json"
    blockers: list[str] = []
    if not manifest_path.exists():
        blockers.append(f"published manifest is missing at {manifest_path}")
        report = {"path": str(path), "publication_status": "missing", "blockers": blockers}
        raise HeightReleaseBlocked(report)

    manifest = json.loads(manifest_path.read_text())
    status = str(
        manifest.get("release_status")
        or manifest.get("publication_status")
        or manifest.get("status")
        or "unknown"
    )
    if status not in {"published", "publication_ready"}:
        blockers.append(f"publication status is {status!r}, not 'published'")
    if manifest.get("publication_blocked") is True:
        blockers.append("publisher marked release publication_blocked")

    missing_count = int(
        manifest.get("missing_height_records", manifest.get("missing_height_count", 0)) or 0
    )
    unresolved_count = int(
        manifest.get(
            "unresolved_representation_components",
            manifest.get("unresolved_representation_count", 0),
        )
        or 0
    )
    if missing_count:
        blockers.append(f"{missing_count:,} component height records are missing")
    if unresolved_count:
        blockers.append(f"{unresolved_count:,} representation components are unresolved")

    epsm = manifest.get("epsm_preflight")
    epsm_status = epsm.get("status") if isinstance(epsm, dict) else epsm
    if epsm_status not in {"passed", "pass", True}:
        blockers.append("EPSM preflight has not passed")

    catalogue_name = manifest.get("component_catalogue") or "component-height-catalogue.parquet"
    catalogue = path / str(catalogue_name)
    if not catalogue.exists():
        blockers.append(f"component catalogue is missing: {catalogue.name}")
        columns: set[str] = set()
    else:
        import pyarrow.parquet as pq

        columns = set(pq.read_schema(catalogue).names)
        missing_columns = sorted(REQUIRED_HEIGHT_COLUMNS - columns)
        if missing_columns:
            blockers.append("component catalogue lacks columns: " + ", ".join(missing_columns))

    checksum_maps = [
        manifest.get("artifact_sha256"),
        manifest.get("file_sha256"),
        manifest.get("checksums"),
        manifest.get("artifacts"),
    ]
    expected_checksum = None
    for mapping in checksum_maps:
        if not isinstance(mapping, dict):
            continue
        value = mapping.get(str(catalogue_name)) or mapping.get(Path(str(catalogue_name)).name)
        if isinstance(value, dict):
            value = value.get("sha256")
        if isinstance(value, str):
            expected_checksum = value
            break
    if catalogue.exists():
        if not expected_checksum:
            blockers.append("published manifest does not pin the component catalogue SHA-256")
        elif sha256_file(catalogue) != expected_checksum:
            blockers.append("component catalogue SHA-256 differs from the published manifest")

    report = {
        "path": str(path),
        "publication_status": status,
        "missing_height_records": missing_count,
        "unresolved_representation_components": unresolved_count,
        "epsm_preflight": epsm_status,
        "component_catalogue": str(catalogue),
        "columns": sorted(columns),
        "component_catalogue_sha256": expected_checksum,
        "blockers": blockers,
        "manifest": manifest,
    }
    if blockers:
        raise HeightReleaseBlocked(report)
    return report


def database_url(explicit: str | None = None) -> str:
    value = explicit or os.environ.get("UBEM_DATABASE_URL")
    if not value:
        raise ValueError("Set UBEM_DATABASE_URL or pass --database-url")
    return value
