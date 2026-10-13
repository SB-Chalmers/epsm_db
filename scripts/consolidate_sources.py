"""Discover, verify, journal and relocate the declared local source closure.

Only exact-byte matches to declared assets are eligible as discovered duplicates.
All original manifests remain immutable; translations live in bundle metadata.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import sqlite3

import pyarrow.parquet as pq

from sweden_ubem.asset_operations import identity, publish_json, relocate, relocation_note
from sweden_ubem.hashing import sha256_file
from sweden_ubem.source_inventory import load_source_inventory

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "data/raw/national-2026-10-13"
FHE = Path.home() / "GitHub/footprint_height_extraction"
TRANSFER = FHE / "outputs/sweden/transfers/tower-national-2026-10-09"
STAGE = FHE / "outputs/sweden/releases/2.0.0/work/component-register-v1"
BASELINE = FHE / "outputs/sweden/harmonized/1.0.0"
REGISTER = FHE / "outputs/sweden/component-register/tower-2026-10-06/register"


def destination_for(key, original):
    path = Path(original)
    if key.startswith("lm_building_geometry"):
        return "lantmateriet/buildings/byggnad_riks.gpkg"
    if key in ("lm_building_archive", "lm_building_metadata"):
        return "lantmateriet/buildings/" + path.name
    if key == "lm_building_xml_root":
        return "lantmateriet/buildings/xml"
    if key.startswith("lm_address_"):
        return "lantmateriet/addresses/" + ("xml" if path.is_dir() else path.name)
    if key == "building_registry_locator":
        return "prepared/register/" + path.name
    if key == "lm_property_geometry":
        return "lantmateriet/properties/" + path.name
    if key.startswith("scb_municipality_boundaries"):
        return "scb/boundaries/" + path.name
    if key.startswith("scb_population"):
        return "scb/population/" + path.name
    if key.startswith("scb_snapshot"):
        return "scb/snapshots/" + path.name
    if key.startswith("height_staging"):
        return "prepared/heights/component-stage/handoff/" + path.name
    if key == "height_path_mapping":
        return "prepared/heights/transfer-receipts/" + path.name
    if key == "tower_component_register_root":
        return "prepared/components"
    if key in ("epc_raw_root", "epc_after_exports"):
        return "boverket/epc" + ("/efter_v260/data" if key == "epc_after_exports" else "")
    if key.startswith("epc_before") or key.startswith("epc_after"):
        return "boverket/epc/" + str(path.relative_to(Path.home() / "GitHub/epc_join/data/epc"))
    if key == "epc_subjects":
        return "boverket/epc/comparison/" + path.name
    if key.startswith("epc_linkage_"):
        return "boverket/epc/comparison/releases/" + path.name
    raise ValueError(f"Unclassified inventory source: {key}")


def journal_connection():
    directory = BUNDLE / "manifests"
    directory.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(directory / "relocation-journal.sqlite")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    c.execute("CREATE TABLE IF NOT EXISTS relocation(original TEXT PRIMARY KEY, destination TEXT NOT NULL, logical TEXT NOT NULL, identity TEXT NOT NULL, group_root TEXT, state TEXT NOT NULL DEFAULT 'prepared')")
    c.commit()
    return c


def discover():
    c = journal_connection()
    if (BUNDLE / "manifests/discovery.json").exists():
        raise ValueError("Discovery already complete; resume relocation instead")
    verified_previous = {original: json.loads(encoded) for original, encoded in c.execute("SELECT original,identity FROM relocation")}
    inventory = load_source_inventory()
    proposed = {}
    keys = {}
    groups = {}
    pins = {}

    def add(path, destination, logical, group=None, expected=None):
        path = Path(path).absolute()
        if path.is_symlink():
            raise ValueError(f"Declared source is a symlink; resolve explicitly: {path}")
        if not path.is_file():
            raise FileNotFoundError(path)
        if str(path) in proposed and proposed[str(path)][0] != destination:
            # Inventory directory entries and individual logical keys may overlap.
            return
        proposed[str(path)] = (destination, logical, str(group) if group else None)
        if expected:
            pins[str(path)] = expected

    for entry in inventory.sources:
        if entry.key in {"sql_migrations", "source_inventory"} or entry.role == "implementation":
            continue
        path = inventory.path_for(entry.key)
        destination = destination_for(entry.key, path)
        keys[entry.key] = destination
        if path.is_dir():
            groups[str(path)] = destination
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    add(child, destination + "/" + str(child.relative_to(path)), entry.key, path)
        elif path.is_file():
            add(path, destination, entry.key, expected=entry.expected_sha256)
        elif entry.status in {"required", "staging_only", "validation_oracle", "archive_only"}:
            raise FileNotFoundError(f"Declared source unavailable: {entry.key}: {path}")

    for snapshot in sorted((REPO / "output/sources/scb").iterdir()):
        if snapshot.is_dir():
            destination = "scb/snapshots/" + snapshot.name
            groups[str(snapshot)] = destination
            for child in snapshot.rglob("*"):
                if child.is_file():
                    add(child, destination + "/" + str(child.relative_to(snapshot)), "historical_scb", snapshot)
    event = REPO / "config/uddevalla-epc-site-events.json"
    add(event, "site-events/assertions/" + event.name, "site_event_assertions")
    for child in (REPO / "output/sources/epc-site-events").glob("*.pdf"):
        add(child, "site-events/documents/" + child.name, "site_event_document")
    for child in TRANSFER.iterdir():
        if child.is_file():
            add(child, "prepared/heights/transfer-receipts/" + child.name, "height_transfer_receipt")
    registry_parent = inventory.path_for("building_registry_locator").parent
    for name in ("provenance.json", "summary.json", "validation.json", "report.md"):
        if (registry_parent / name).is_file():
            add(registry_parent / name, "prepared/register/" + name, "registry_preparation_provenance")
    transferred = json.loads((TRANSFER / "local-files.json").read_text())
    for item in transferred["files"]:
        path = Path(item["destination"])
        if path.is_relative_to(REGISTER):
            destination = "prepared/components/" + str(path.relative_to(REGISTER))
        elif path.is_relative_to(STAGE):
            destination = "prepared/heights/component-stage/" + str(path.relative_to(STAGE))
        else:
            destination = "prepared/heights/supporting/" + item["relative_path"]
        add(path, destination, item["kind"], expected=item["sha256"])

    references = {}
    catalogue = inventory.path_for("height_staging_catalogue")
    for batch in pq.ParquetFile(catalogue).iter_batches(batch_size=8192, columns=[
        "evidence_root", "evidence_record_path", "evidence_record_sha256"]):
        for row in batch.to_pylist():
            if not row["evidence_record_path"]:
                continue
            key = (row["evidence_root"], row["evidence_record_path"])
            previous = references.get(key)
            if previous and previous != row["evidence_record_sha256"]:
                raise ValueError("Conflicting height measurement hash")
            references[key] = row["evidence_record_sha256"]
    translations = []
    for (root, relative), expected in references.items():
        if root == "immutable_baseline":
            path = BASELINE / relative
            destination = "prepared/heights/immutable-baseline/" + relative
        else:
            path = Path(relative)
            if not path.is_absolute():
                path = STAGE / path
            if str(path).startswith("/home/csd-lab/Documents/github/footprint_height_extraction/"):
                path = FHE / path.relative_to("/home/csd-lab/Documents/github/footprint_height_extraction")
            if not path.is_relative_to(STAGE):
                raise ValueError(f"Unmapped component measurement: {path}")
            destination = "prepared/heights/component-stage/" + str(path.relative_to(STAGE))
        add(path, destination, "height_measurement", expected=expected)
        translations.append({"evidence_root": root, "original_path": relative,
                             "bundle_path": destination, "sha256": expected})
    if len(references) != 63842:
        raise ValueError(f"Height reference closure differs: {len(references)}")
    # Preserve the baseline release's supporting receipts and manifest if present.
    for name in ("manifest.json", "release.json", "metadata.json", "reconciliation.json"):
        path = BASELINE / name
        if path.is_file():
            add(path, "prepared/heights/immutable-baseline/" + name, "height_baseline_provenance")

    known = defaultdict(set)
    candidates = {}
    identities = {}
    hash_cache = {}
    for number, (original, (destination, logical, group)) in enumerate(sorted(proposed.items()), 1):
        stat = identity(original, checksum=False)
        cache_key = (stat["device"], stat["inode"], stat["bytes"], stat["mtime_ns"])
        old = verified_previous.get(original)
        if old and {key: value for key, value in old.items() if key != "sha256"} != stat:
            raise ValueError(f"Previously verified source identity changed: {original}")
        digest = old["sha256"] if old else hash_cache.setdefault(cache_key, None)
        if digest is None:
            digest = hash_cache[cache_key] = sha256_file(Path(original))
        stat["sha256"] = digest
        if original in pins and pins[original] != digest:
            raise ValueError(f"Declared checksum mismatch: {original}")
        identities[original] = stat
        known[(Path(original).name, stat["bytes"])].add(digest)
        candidates[(Path(original).name, stat["bytes"], digest)] = original
        c.execute("INSERT OR IGNORE INTO relocation(original,destination,logical,identity,group_root) VALUES(?,?,?,?,?)",
                  (original, destination, logical, json.dumps(stat), group))
        if number % 512 == 0:
            c.commit()
            print(f"Verified declared source files: {number}/{len(proposed)}", flush=True)
    c.commit()
    coverage = {"roots": [], "excluded": [], "inaccessible": [], "different_content": []}
    excluded_names = {".git", ".venv", "node_modules", "CloudStorage", "Mobile Documents", ".Trash"}
    scan_roots = [Path.home(), Path("/Users/Shared")]
    # Local mounted volumes are included; remote and cloud mounts are never followed.
    import subprocess
    for line in subprocess.run(["/sbin/mount"], check=True, capture_output=True, text=True).stdout.splitlines():
        if " on /Volumes/" in line and any(t in line for t in ("(apfs", "(hfs", "(exfat", "(msdos")):
            scan_roots.append(Path(line.split(" on ", 1)[1].split(" (", 1)[0]))
    for scan_root in scan_roots:
        coverage["roots"].append(str(scan_root))
        def inaccessible(error):
            coverage["inaccessible"].append(str(error.filename))
        for directory, names, files in os.walk(scan_root, followlinks=False, onerror=inaccessible):
            base = Path(directory)
            for name in list(names):
                child = base / name
                if name in excluded_names or child.is_symlink() or child.is_relative_to(REPO / "output") or child == BUNDLE:
                    coverage["excluded"].append(str(child))
                    names.remove(name)
            for name in files:
                path = base / name
                if str(path) in proposed or name.endswith(".RELOCATED.txt") or path.is_symlink():
                    continue
                try:
                    size = path.stat().st_size
                    matches = known.get((name, size))
                    if not matches:
                        continue
                    stat = identity(path)
                    if stat["sha256"] not in matches:
                        coverage["different_content"].append(str(path))
                        continue
                    original = candidates[(name, size, stat["sha256"])]
                    destination = proposed[original][0]
                    c.execute("INSERT OR IGNORE INTO relocation(original,destination,logical,identity) VALUES(?,?,?,?)",
                              (str(path), destination, "discovered_exact_duplicate", json.dumps(stat)))
                    c.commit()
                except (OSError, PermissionError) as error:
                    coverage["inaccessible"].append(str(path))
    c.commit()
    publish_json(BUNDLE / "manifests/discovery-coverage.json", coverage)
    publish_json(BUNDLE / "manifests/inventory-paths.json", keys)
    publish_json(BUNDLE / "manifests/directory-relocations.json", groups)
    publish_json(BUNDLE / "manifests/height-assets.json", {"measurement_units": len(references), "assets": translations})
    report = {"files": c.execute("SELECT count(*) FROM relocation").fetchone()[0],
              "height_measurement_units": len(references), "discovery_complete": True}
    publish_json(BUNDLE / "manifests/discovery.json", report)
    print(json.dumps(report), flush=True)


def execute():
    if not (BUNDLE / "manifests/discovery.json").is_file():
        raise ValueError("Complete discovery is required before relocation")
    c = journal_connection()
    assets = {}
    occurrences = []
    groups = defaultdict(list)
    records = c.execute("SELECT original,destination,logical,identity,group_root,state FROM relocation ORDER BY original").fetchall()
    for number, (original, destination, logical, encoded, group, state) in enumerate(records, 1):
        stat = json.loads(encoded)
        digest = stat["sha256"]
        target = BUNDLE / destination
        canonical = assets.get(digest)
        if canonical and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(BUNDLE / canonical, target)
        entry = {"original": original, "destination": destination, "identity": stat}
        relocate(entry, BUNDLE, note=not group)
        assets.setdefault(digest, destination)
        occurrences.append({"original": original, "path": destination, "canonical": assets[digest],
                            "logical_identity": logical, "bytes": stat["bytes"], "sha256": digest})
        if group:
            groups[group].append(occurrences[-1])
        c.execute("UPDATE relocation SET state='complete' WHERE original=?", (original,))
        if number % 128 == 0:
            c.commit()
        if number % 512 == 0:
            print(f"Relocated verified source files: {number}/{len(records)}", flush=True)
    c.commit()
    directory_map = json.loads((BUNDLE / "manifests/directory-relocations.json").read_text())
    for group, relative in directory_map.items():
        items = [dict(item, current_destination=str(BUNDLE / item["path"]),
                      bundle_relative_path=item["path"],relocation_date="2026-10-13")
                 for item in occurrences if Path(item["original"]).is_relative_to(group)]
        root = Path(group)
        root.mkdir(parents=True, exist_ok=True)
        (root / "RELOCATED.txt").write_text(
            f"Original source directory: {root}\nCurrent absolute destination: {BUNDLE / relative}\n"
            f"Bundle-relative directory: {relative}\nRelocation date: 2026-10-13\n"
            "See RELOCATED-INDEX.json for every original file, destination and SHA-256.\n"
            "Consumers must configure the new source root. No compatibility symlinks are provided.\n")
        publish_json(root / "RELOCATED-INDEX.json", items)
    publish_json(BUNDLE / "manifests/file-inventory.json", {"files": occurrences,
                  "unique_assets": len(assets), "physical_deduplication": "bundle-internal hard links"})
    publish_json(BUNDLE / "manifests/relocation-journal.json", {"files": occurrences})
    print(json.dumps({"relocated_occurrences": len(occurrences), "unique_assets": len(assets)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["discover", "relocate"])
    args = parser.parse_args()
    discover() if args.action == "discover" else execute()
