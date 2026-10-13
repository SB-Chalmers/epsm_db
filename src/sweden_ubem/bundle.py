"""Portable source references and verified translations for immutable manifests."""
import json
from pathlib import Path

from .hashing import sha256_file
from .asset_operations import safe_relative


def bundle_root(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if (parent / "manifests/file-inventory.json").is_file():
            return parent
    return None


def translated_reference(manifest, reference, *, source_root=None, expected_sha256=None):
    root = Path(source_root) if source_root else bundle_root(manifest)
    if root is None:
        candidate = Path(manifest).parent / reference
    else:
        inventory = json.loads((root / "manifests/file-inventory.json").read_text())["files"]
        # Match the original manifest-relative path through its preserved original location.
        originals = [item["original"] for item in inventory
                     if item["path"] == str(Path(manifest).relative_to(root))]
        if not originals:
            raise ValueError("Manifest original location is ambiguous")
        original_paths = {str((Path(original).parent / reference).resolve()) for original in originals}
        matches = {item["path"] for item in inventory if item["original"] in original_paths}
        if len(matches) != 1:
            raise ValueError(f"Referenced file is outside bundle closure: {reference}")
        candidate = root / safe_relative(matches.pop())
    if not candidate.is_file():
        raise FileNotFoundError(f"Referenced source file missing: {candidate}")
    if expected_sha256 and sha256_file(candidate) != expected_sha256:
        raise ValueError(f"Referenced source checksum differs: {candidate}")
    return candidate


def verify_bundle(root):
    root = Path(root).resolve()
    document = json.loads((root / "manifests/file-inventory.json").read_text())
    checked = {}
    expected_paths = set()
    expected_hashes = {}
    for item in document["files"]:
        relative = safe_relative(item["path"])
        expected_paths.add(str(relative))
        if str(relative) in expected_hashes and expected_hashes[str(relative)] != item["sha256"]:
            raise ValueError("Conflicting logical asset hashes")
        expected_hashes[str(relative)] = item["sha256"]
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError(f"Missing or escaping bundle asset: {relative}")
        stat = path.stat()
        key = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if key not in checked:
            checked[key] = sha256_file(path)
        if stat.st_size != item["bytes"] or checked[key] != item["sha256"]:
            raise ValueError(f"Bundle checksum differs: {relative}")
    actual = {str(p.relative_to(root)) for p in root.rglob("*")
              if p.is_file() and not p.relative_to(root).parts[0] == "manifests"}
    if actual != expected_paths:
        raise ValueError("Bundle file closure differs from inventory")
    heights = json.loads((root / "manifests/height-assets.json").read_text())
    if heights["measurement_units"] != 63842 or len(heights["assets"]) != 63842:
        raise ValueError("Height measurement closure differs")
    identities=set()
    for item in heights["assets"]:
        if item["bundle_path"] not in expected_paths:
            raise ValueError("Height dependency missing from file inventory")
        if item["sha256"] != expected_hashes[item["bundle_path"]]:
            raise ValueError("Height dependency hash differs from file inventory")
        key=(item["evidence_root"],item["original_path"])
        if key in identities:
            raise ValueError("Duplicate logical height dependency")
        identities.add(key)
    import pyarrow.parquet as pq
    components = root / "prepared/components"
    manifest = json.loads((components / "manifest.json").read_text())
    count = sum(pq.ParquetFile(p).metadata.num_rows for p in (components / "components").glob("*.parquet"))
    lineage = pq.ParquetFile(components / "source-lineage.parquet").metadata.num_rows
    if count != manifest["component_count"] or lineage != manifest["source_row_count"]:
        raise ValueError("Prepared component/lineage row counts differ")
    from .config import validate_height_staging
    catalogue = root / "prepared/heights/component-stage/handoff/component-height-catalogue.parquet"
    staging = validate_height_staging(catalogue, geometry_register=components)
    return {"verified_occurrences": len(document["files"]), "verified_physical_assets": len(checked),
            "height_measurement_units": len(heights["assets"]), "components": count, "lineage_rows": lineage,
            "height_publication_blockers": staging["blockers"], "height_production_promotable": False}
