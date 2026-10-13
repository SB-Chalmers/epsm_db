"""Checks for the reviewed documentation, not acceptance of the legacy DB implementation."""

import re
from pathlib import Path

from scripts.sync_manual_inventory import scoped_path_appendix


ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), ROOT / "notebooks/README.md")


def test_local_documentation_links_and_editable_diagrams_exist():
    for path in DOCS:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if target.startswith(("http://", "https://", "#")):
                continue
            relative = target.split("#", 1)[0]
            candidate = (path.parent / relative).resolve()
            # Generated previews are not checked in; the corresponding Mermaid
            # source must exist so CI can rebuild them without national data.
            if candidate.parent == ROOT / "output/diagrams":
                assert (ROOT / "docs/diagrams" / (candidate.stem + ".mmd")).is_file()
            else:
                # Sibling source-contract links are optional on a CI checkout.
                if not candidate.is_relative_to(ROOT):
                    continue
                assert candidate.exists(), f"{path}: broken link {target}"


def test_current_docs_do_not_reintroduce_superseded_dataset_instructions():
    for path in DOCS:
        text = path.read_text()
        assert "toy-gbg-v" not in text
        assert "The model in plain language" not in text
        assert "Generated exact path appendix" not in text
    manual = (ROOT / "docs/ubem_database_design_and_setup.md").read_text()
    assert "--municipality 1485" in manual
    assert "Approval date" in manual
    assert "dataset plan" in manual


def test_all_source_rules_have_checks_authorities_and_failure_behavior():
    text = (ROOT / "docs/validation_rules.md").read_text()
    rows = [line for line in text.splitlines() if re.match(r"\| [DP]\d{2} ", line)]
    assert len(rows) == 23
    identifiers = []
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert len(cells) == 4
        assert all(cells)
        identifiers.append(cells[0].split()[0])
    assert len(set(identifiers)) == len(identifiers)
    assert "not the exact schema for every delivered product" in text
    assert "mapping pending" in text


def test_documented_paths_ignore_shell_mount_overrides(monkeypatch):
    expected = scoped_path_appendix()
    monkeypatch.setenv("UBEM_SOURCE_ROOT", "/a/temporary/source/mount")
    monkeypatch.setenv("UBEM_REPO_ROOT", "/a/temporary/repo/mount")
    assert scoped_path_appendix() == expected
