"""Checks for the reviewed lake documentation; the retired pipeline is under archive/."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")))


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
            elif candidate.is_relative_to(ROOT / "output"):
                # Local run evidence is ignored by Git and absent from CI clones.
                continue
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
    for path in DOCS:
        assert "sweden-ubem db init" not in path.read_text(), path
