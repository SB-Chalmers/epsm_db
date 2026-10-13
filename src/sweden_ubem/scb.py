"""Acquire and validate immutable SCB spatial-context snapshots."""

from __future__ import annotations

import json
import shutil
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .hashing import canonical_json, content_hash, sha256_file

WFS_URL = "https://geodata.scb.se/geoserver/stat/wfs"
PXWEB_URL = "https://api.scb.se/OV0104/v1/doris/sv/ssd/HE/HE0110/HE0110I/Tab1InkDesoRegso"
POPULATION_LAYER = "befolkning_1km_2025"
POPULATION_FIELDS = (
    "beftotalt",
    "man",
    "kvinna",
    "ald0_4",
    "ald5_9",
    "ald10_14",
    "ald15_19",
    "ald20_24",
    "ald25_29",
    "ald30_34",
    "ald35_39",
    "ald40_44",
    "ald45_49",
    "ald50_54",
    "ald55_59",
    "ald60_64",
    "ald65_69",
    "ald70_74",
    "ald75_79",
    "ald80_84",
    "ald85_89",
    "ald90_94",
    "ald95_99",
    "ald100w",
)

Transport = Callable[[str, str, bytes | None, dict[str, str]], tuple[bytes, dict[str, str]]]


def _transport(
    method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 - fixed SCB URLs
        return response.read(), {key.lower(): value for key, value in response.headers.items()}


def default_snapshot_root() -> Path:
    return Path(__file__).resolve().parents[2] / "output/sources/scb"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _regso_code(properties: dict[str, Any]) -> str | None:
    for key in ("regsokod", "RegSO_kod", "regso_code", "REGSOKOD"):
        if properties.get(key):
            return str(properties[key]).replace("_RegSO2025", "")
    return None


def _stable_wfs_content(document: dict[str, Any]) -> dict[str, Any]:
    """Remove GeoServer response-time noise while retaining all source features."""
    stable = {key: value for key, value in document.items() if key != "timeStamp"}
    stable["features"] = sorted(
        document.get("features") or [],
        key=lambda feature: (
            _regso_code(feature.get("properties") or {}) or "",
            str(feature.get("id") or ""),
        ),
    )
    return stable


def acquire_scb_snapshot(
    *,
    population_grid: Path,
    municipality: str = "1480",
    output_root: Path | None = None,
    regso_year: int = 2025,
    income_year: int = 2024,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Acquire WFS/PxWeb responses once and write a content-addressed snapshot.

    The national population GeoPackage is referenced in place. No API is called by
    database ingestion or by ``dataset load``.
    """
    population_grid = population_grid.expanduser().resolve()
    if not population_grid.is_file():
        raise FileNotFoundError(population_grid)
    municipality = str(municipality).zfill(4)
    output_root = (output_root or default_snapshot_root()).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    call = transport or _transport
    import pyogrio

    population_info = pyogrio.read_info(population_grid, layer=POPULATION_LAYER)

    wfs_params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"stat:RegSO_{regso_year}",
        "outputFormat": "application/json",
        "srsName": "EPSG:3006",
        "CQL_FILTER": f"kommunkod='{municipality}'",
    }
    wfs_url = WFS_URL + "?" + urllib.parse.urlencode(wfs_params)
    regso_body, regso_headers = call("GET", wfs_url, None, {"Accept": "application/json"})
    regso_json = json.loads(regso_body)
    features = regso_json.get("features") or []
    codes = sorted(
        {
            code
            for feature in features
            if (code := _regso_code(feature.get("properties") or {})) is not None
        }
    )
    if not codes:
        raise ValueError(f"SCB WFS returned no RegSO codes for municipality {municipality}")

    pxweb_query = {
        "query": [
            {
                "code": "Region",
                "selection": {
                    "filter": f"vs:RegSO{regso_year}",
                    "values": [f"{code}_RegSO{regso_year}" for code in codes],
                },
            },
            {"code": "InkomstTyp", "selection": {"filter": "item", "values": ["SaFörInk"]}},
            {"code": "Kon", "selection": {"filter": "item", "values": ["1+2"]}},
            {"code": "ContentsCode", "selection": {"filter": "item", "values": ["0000089U"]}},
            {"code": "Tid", "selection": {"filter": "item", "values": [str(income_year)]}},
        ],
        "response": {"format": "json"},
    }
    request_body = canonical_json(pxweb_query).encode("utf-8")
    income_body, income_headers = call(
        "POST",
        PXWEB_URL,
        request_body,
        {"Accept": "application/json", "Content-Type": "application/json"},
    )
    income_json = json.loads(income_body)
    if not isinstance(income_json.get("data"), list):
        raise ValueError("SCB PxWeb response lacks a data array")

    retrieved_at = datetime.now(timezone.utc).isoformat()
    identity = {
        "acquisition_contract": "scb-context-acquisition-v1.2",
        "municipality": municipality,
        "population_sha256": sha256_file(population_grid),
        "regso_request": wfs_params,
        "regso_response_content_sha256": content_hash(_stable_wfs_content(regso_json)),
        "income_request": pxweb_query,
        "income_response_sha256": content_hash(income_json),
    }
    snapshot_id = content_hash(identity)
    target = output_root / snapshot_id
    if target.exists():
        return validate_scb_snapshot(target)

    temporary = Path(tempfile.mkdtemp(prefix=".scb-", dir=output_root))
    try:
        _write_json(temporary / "regso-request.json", wfs_params)
        _write_json(temporary / "regso-response.geojson", regso_json)
        _write_json(temporary / "income-request.json", pxweb_query)
        _write_json(temporary / "income-response.json", income_json)
        manifest = {
            "schema_version": "scb-context-snapshot-v1",
            "snapshot_id": snapshot_id,
            "municipality": municipality,
            "retrieved_at": retrieved_at,
            "population_grid": {
                "path": str(population_grid),
                "sha256": identity["population_sha256"],
                "byte_size": population_grid.stat().st_size,
                "layer": POPULATION_LAYER,
                "reference_period": "2025-12-31",
                "crs": "EPSG:3006",
                "publisher": "Statistics Sweden",
                "license": "CC0-1.0",
                "schema": dict(
                    zip(population_info["fields"].tolist(), population_info["dtypes"].tolist())
                ),
            },
            "regso": {
                "year": regso_year,
                "endpoint": WFS_URL,
                "request_file": "regso-request.json",
                "response_file": "regso-response.geojson",
                "feature_count": len(features),
                "area_codes": codes,
                "response_sha256": sha256_file(temporary / "regso-response.geojson"),
                "request_sha256": sha256_file(temporary / "regso-request.json"),
                "response_headers": regso_headers,
                "publisher": "Statistics Sweden",
                "license": "CC0-1.0",
            },
            "income": {
                "year": income_year,
                "endpoint": PXWEB_URL,
                "dataset_code": "HE0110I/Tab1InkDesoRegso",
                "contents_code": "0000089U",
                "request_file": "income-request.json",
                "response_file": "income-response.json",
                "request_sha256": sha256_file(temporary / "income-request.json"),
                "response_sha256": sha256_file(temporary / "income-response.json"),
                "response_headers": income_headers,
                "publisher": "Statistics Sweden",
                "license": "CC0-1.0",
            },
            "publication_status": "published",
        }
        _write_json(temporary / "manifest.json", manifest)
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return validate_scb_snapshot(target)


def validate_scb_snapshot(path: Path) -> dict[str, Any]:
    """Validate hashes and minimum source contracts without network access."""
    path = path.expanduser().resolve()
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"SCB snapshot manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != "scb-context-snapshot-v1":
        raise ValueError("Unsupported SCB snapshot contract")
    from .bundle import translated_reference

    population = translated_reference(
        manifest_path, manifest["population_grid"]["path"],
        expected_sha256=manifest["population_grid"]["sha256"],
    )
    if sha256_file(population) != manifest["population_grid"]["sha256"]:
        raise ValueError("SCB population GeoPackage checksum changed")
    for group, key in (
        ("regso", "request_file"),
        ("regso", "response_file"),
        ("income", "request_file"),
        ("income", "response_file"),
    ):
        source = path / manifest[group][key]
        expected = manifest[group][key.replace("file", "sha256")]
        if sha256_file(source) != expected:
            raise ValueError(f"SCB snapshot checksum changed: {source.name}")
    import pyogrio

    info = pyogrio.read_info(population, layer=manifest["population_grid"]["layer"])
    fields = set(info["fields"])
    required = {"objectid", "rutid_scb", "rutstorl", "referenstid", *POPULATION_FIELDS}
    missing = sorted(required - fields)
    if missing or info["crs"] != "EPSG:3006":
        raise ValueError(
            f"Population grid source contract failed; missing={missing}, crs={info['crs']}"
        )
    regso = json.loads((path / manifest["regso"]["response_file"]).read_text())
    if len(regso.get("features") or []) != manifest["regso"]["feature_count"]:
        raise ValueError("RegSO feature count differs from manifest")
    income = json.loads((path / manifest["income"]["response_file"]).read_text())
    if not isinstance(income.get("data"), list):
        raise ValueError("PxWeb response lacks data")
    return {**manifest, "path": str(path), "ready": True}


def latest_scb_snapshot(root: Path | None = None) -> Path:
    root = (root or default_snapshot_root()).expanduser().resolve()
    candidates = sorted(
        (path for path in root.glob("*/manifest.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No pinned SCB snapshot under {root}; run 'sweden-ubem source acquire-scb'"
        )
    return candidates[0].parent
