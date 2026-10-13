"""Refresh the scoped documentation appendix, without changing runtime configuration."""

from pathlib import Path
from string import Template

from sweden_ubem.source_inventory import load_source_inventory


START = "<!-- SOURCE-INVENTORY:START -->"
END = "<!-- SOURCE-INVENTORY:END -->"

# Do not list application paths as national source dependencies.
EXCLUDED_KEYS = {
    "component_register_root",
    "published_height_release_root",
    "epsm_parametric_catalogue",
    "epsm_dataset_root",
    "sql_migrations",
    "ontology",
    "shacl_shapes",
    "epc_field_dictionary",
    "toy_staging_output",
    "export_output",
}


def scoped_path_appendix() -> str:
    inventory = load_source_inventory()
    # Documentation records the configured profile, not the invoking shell's
    # overrides. Otherwise a temporary source mount would rewrite the manual.
    variables = inventory.profiles["mac-local"].variables
    lines = ["| Input | Local path |", "|---|---|"]
    for source in inventory.sources:
        if source.key in EXCLUDED_KEYS:
            continue
        label = source.key
        if source.key == "scb_snapshot_pinned":
            label += " (Göteborg only; not Uddevalla)"
        elif source.status == "validation_oracle":
            label += " (comparison only)"
        elif source.status == "staging_only":
            label += " (unpublished staging)"
        path = Template(source.path_template).substitute(variables)
        lines.append(f"| `{label}` | `{path}` |")
    return "\n".join(lines)


def synchronized_text(text: str) -> str:
    before, separator, remainder = text.partition(START)
    if not separator:
        raise ValueError(f"Manual lacks {START}")
    _old, separator, after = remainder.partition(END)
    if not separator:
        raise ValueError(f"Manual lacks {END}")
    generated = scoped_path_appendix()
    return f"{before}{START}\n{generated}\n{END}{after}"


def main() -> None:
    path = Path(__file__).resolve().parents[1] / "docs/source_paths.md"
    original = path.read_text()
    updated = synchronized_text(original)
    if original != updated:
        path.write_text(updated)


if __name__ == "__main__":
    main()
