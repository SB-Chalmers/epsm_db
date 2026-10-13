"""Build and checksum the self-contained completed-build preservation set.

Never deletes input artifacts. Restore verification is deliberately separate.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "output/retained/uddevalla-2026-10-12"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def preserve():
    DEST.mkdir(parents=True, exist_ok=True)
    locations = []
    shared = {}

    def retain(source, relative):
        source = Path(source)
        target = DEST / relative
        if source.is_dir():
            for child in sorted(source.iterdir()):
                retain(child, Path(relative) / child.name)
            return
        actual = digest(source)
        if target.exists():
            if digest(target) != actual:
                raise ValueError(f"Conflicting retained file: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Materialize symlinks; hard links are solely within the retention set.
            if actual in shared:
                os.link(shared[actual], target)
            else:
                shutil.copy2(source, target, follow_symlinks=True)
        shared[actual] = target
        locations.append({"original": str(source), "retained": str(relative),
                          "sha256": actual, "bytes": target.stat().st_size})

    for source, relative in [
        ("output/validation/uddevalla-2026-10-12", "completed-build"),
        ("output/validation/reimplementation-baseline-2026-10-12", "baseline/original"),
        ("output/validation/reimplementation-baseline-ewkb-2026-10-13", "baseline/full-geometry"),
        ("output/verification", "example-exports"),
        ("output/notebooks", "executed-notebooks"),
        ("output/pdf/uddevalla-epc-implementation-evaluation-2026-10-12.pdf", "evaluation.pdf"),
        ("output/sources/application", "application-snapshots"),
        ("output/backups/reimplementation-code-2026-10-12.tar.gz", "historical-code.tar.gz"),
        ("config", "configuration-at-restart"),
        ("notebooks", "notebook-sources"),
    ]:
        retain(ROOT / source, Path(relative))

    with tarfile.open(DEST / "historical-code.tar.gz") as archive:
        member = archive.extractfile("docs/epc_matching_and_reporting.md")
        data = member.read()
        expected = "991dea5d577f029b7154c580d45b383f40e3189ce0d542cdfe48b7cc97c0af1a"
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Historical document checksum mismatch")
        target = DEST / "historical-documents/epc_matching_and_reporting.md"
        target.parent.mkdir(exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise ValueError("Conflicting historical document")
        target.write_bytes(data)

    frozen = DEST / "benchmark-loader-v003.tar.gz"
    if not frozen.exists():
        with tarfile.open(frozen, "w:gz") as archive:
            for directory in ("src", "sql", "scripts", "config", "docs", "tests", "notebooks", ".github"):
                for path in sorted((ROOT / directory).rglob("*")):
                    if path.is_file() and "__pycache__" not in path.parts and ".egg-info" not in str(path):
                        archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
            for name in ("pyproject.toml", "README.md", ".env.example", ".gitignore"):
                archive.add(ROOT / name, arcname=name)

    (DEST / "locations.json").write_text(json.dumps(locations, indent=2) + "\n")
    (DEST / "REPRODUCE.md").write_text(
        "# Completed Uddevalla, 12 October 2026\n\n"
        "The protected database is sweden_ubem on Docker port 55432. Its public stock "
        "contains 45,300 buildings and 47,287 components. Assessment date: 2026-10-12.\n\n"
        "Verify checksum-manifest.json before use. Restore baseline/original/database.dump "
        "with PostgreSQL 17 pg_restore --no-owner into a new disposable PostGIS database; "
        "never restore over the completed database. The two dump paths are hard links to "
        "one physical dump. Use baseline/full-geometry for keyed public comparisons. "
        "Historical manifests remain verbatim; locations.json records relocated artifacts.\n\n"
        "Restore verification is recorded in restore-verification.json. The application "
        "ZIPs are those pinned by recorded ingestion runs; historical-code.tar.gz includes "
        "the exact report document and supporting configuration/scripts. "
        "benchmark-loader-v003.tar.gz freezes the pre-refactor implementation.\n\n"
        "Original national source files are preserved separately in the portable raw bundle; "
        "configure UBEM_SOURCE_ROOT for original inspection or a fresh source import. "
        "Restored processing, exploration, reports and exports use PostgreSQL.\n"
    )
    checksum()
    return {"retained": str(DEST), "artifacts": len(locations), "checksums_verified": True}


def checksum():
    files = []
    for path in sorted(DEST.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"External preservation dependency: {path}")
        if path.is_file() and path.name != "checksum-manifest.json":
            files.append({"path": str(path.relative_to(DEST)), "bytes": path.stat().st_size,
                          "sha256": digest(path)})
    manifest = DEST / "checksum-manifest.json"
    manifest.write_text(json.dumps({"files": files}, indent=2) + "\n")
    verify()


def verify():
    manifest = json.loads((DEST / "checksum-manifest.json").read_text())
    expected = {item["path"] for item in manifest["files"]}
    actual = {str(p.relative_to(DEST)) for p in DEST.rglob("*")
              if p.is_file() and p.name != "checksum-manifest.json"}
    if actual != expected:
        raise ValueError("Preservation file closure changed")
    for item in manifest["files"]:
        path = DEST / item["path"]
        if path.is_symlink() or path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise ValueError(f"Preservation checksum mismatch: {path}")


if __name__ == "__main__":
    print(json.dumps(preserve(), indent=2))
