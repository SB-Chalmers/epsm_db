"""Remove explicitly enumerated generated artifacts after retention and relocation gates."""
import json
import re
import shutil
import subprocess
from pathlib import Path

from sweden_ubem.asset_operations import identity, relocate, deletion_manifest, delete_listed, publish_json
from scripts.preserve_uddevalla import ROOT, DEST, verify

RESTART = ROOT / "output/restart-2026-10-13"
BUNDLE = ROOT / "data/raw/national-2026-10-13"

VALIDATION = """
database-design-review-2026-10-13.json ingestion-batch-benchmark.json ingestion-batch-benchmark.log
ingestion-performance-diagnosis.json ingestion-status-tests.log national-import-v003-resources.jsonl
national-import-v003.log native-pg15-init.log native-pg15-regression-tests.log native-pg15-restore-check
native-pg15-restore-check.log native-pg15-server.log native-pg17-acceptance native-pg17-baseline-geometry.log
native-pg17-build native-pg17-field-definition-audit.json native-pg17-implementation.json
native-pg17-install.log native-pg17-stale-cleanup.json native-pg17-storage-audit.json native-pg17-tests-final.log
native-pg17-transfer native-postgis-dependencies.log native-postgres-available-versions.json
native-postgresql15-assessment.json native-test-cleanup.json native-vs-container-benchmark
native-vs-container-benchmark.log original-physical-assessment.json parity-comparator-self-check
parity-comparator-self-check.log parity-ewkb-self-check parity-ewkb-self-check.log
processing-key-development-adjustment.json redundant-component-index-removal.json
reimplementation-baseline-2026-10-12 reimplementation-baseline-ewkb-2026-10-13
reimplementation-lint.log reimplementation-real-source-acceptance reimplementation-real-source-acceptance.log
reimplementation-regression-tests.log reimplementation-status.json reimplementation-supervised-run.log
uddevalla-2026-10-12 uddevalla-epc-comparison-2026-10-10.json
""".split()


def cleanup():
    verify()
    report = json.loads((RESTART / "bundle-verification.json").read_text())
    if report["height_measurement_units"] != 63842 or report["components"] != 9464839:
        raise ValueError("Verified bundle is required before generated-artifact cleanup")
    for name in ("database-design-review-2026-10-13.json", "native-pg17-storage-audit.json",
                 "ingestion-performance-diagnosis.json", "processing-key-development-adjustment.json"):
        source = ROOT / "output/validation" / name
        if source.is_file():
            (RESTART / "historical-diagnostics").mkdir(exist_ok=True)
            shutil.copy2(source, RESTART / "historical-diagnostics" / name)
    # Pinned application snapshots belong to preservation, rather than raw ingestion.
    application = ROOT / "output/sources/application"
    notes = []
    for source in sorted(application.glob("*.zip")):
        entry = {"original": str(source), "destination": "application-snapshots/" + source.name,
                 "identity": identity(source)}
        target = relocate(entry, DEST)
        notes.append(dict(original=str(source),current_destination=str(target),
                          preservation_relative_path=entry["destination"],sha256=entry["identity"]["sha256"],relocation_date="2026-10-13"))
    if notes:
        (application / "RELOCATED.txt").write_text(f"Pinned application snapshots are retained at {DEST}/application-snapshots. See RELOCATED-INDEX.json. Relocation date: 2026-10-13.\n")
        publish_json(application / "RELOCATED-INDEX.json",notes)
    targets = [ROOT / "output/validation" / name for name in VALIDATION]
    targets += [ROOT / "output/backups" / name for name in (
        "before-height-provenance-correction-2026-10-10.dump", "before-v001-alignment-2026-10-10.dump",
        "native-pipeline-2026-10-13.tar.gz", "native-postgis-3.5.2.tar.gz",
        "reimplementation-code-2026-10-12.tar.gz", "sweden-ubem-before-epc-v2-2026-10-12.dump")]
    targets += [ROOT / name for name in (
        "output/transfers/national-v003-2026-10-12", "output/verification", "output/notebooks",
        "output/pdf/uddevalla-epc-implementation-evaluation-2026-10-12.pdf", ".pytest_cache", ".ruff_cache", "tmp")]
    targets += [ROOT / "scripts" / name for name in ("adopt_execution_revision.py","verify_native_transfer.py")]
    # Cache directories are enumerated, not blanket deletion of source/test directories.
    targets += [p for base in (ROOT / "src",ROOT / "scripts",ROOT / "tests") for p in base.rglob("__pycache__")]
    targets = [p for p in targets if p.exists() or p.is_symlink()]
    process_list = subprocess.run(["ps","ax","-o","command="],check=True,capture_output=True,text=True).stdout
    for target in targets:
        names=(str(target),str(target.relative_to(ROOT)))
        if any(re.search(r"(?<![\w/.-])"+re.escape(name)+r"(?=$|[\s/'\"])",process_list) for name in names):
            raise ValueError(f"Generated artifact still used by a process: {target}")
    protected = [DEST,BUNDLE,ROOT / "output/sources",RESTART,ROOT / "output/runtime/native-pg17-v004"]
    manifest = deletion_manifest(targets,protected=protected)
    publish_json(RESTART / "generated-deletion-manifest.json",manifest)
    bytes_listed = sum(item["identity"]["bytes"] for item in manifest["files"] if "sha256" in item["identity"])
    delete_listed(manifest,targets)
    publish_json(RESTART / "generated-cleanup.json",dict(targets=len(targets),listed_file_bytes=bytes_listed,completed=True))
    return {"removed_targets":len(targets),"listed_file_bytes":bytes_listed}


if __name__ == "__main__":
    print(json.dumps(cleanup(),indent=2))
