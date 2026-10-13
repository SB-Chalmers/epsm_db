from __future__ import annotations

import os
import string
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .hashing import sha256_file


INVENTORY_PATH = Path(__file__).resolve().parents[2] / "config/source_inventory.toml"
DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "data/raw/national-2026-10-13"
VALID_STATUSES = {
    "required",
    "candidate",
    "validation_oracle",
    "scenario_reference",
    "archive_only",
    "generated",
    "staging_only",
}


@dataclass(frozen=True)
class SourceEntry:
    key: str
    logical_name: str
    dataset: str
    status: str
    role: str
    contract: str
    path_template: str
    expected_sha256: str | None = None


@dataclass(frozen=True)
class SourceProfile:
    name: str
    description: str
    variables: dict[str, str]


@dataclass(frozen=True)
class ResolvedSource:
    entry: SourceEntry
    profile: str
    path: Path
    path_kind: str
    exists: bool
    byte_size: int | None
    sha256: str | None
    checksum_status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.entry.key,
            "logical_name": self.entry.logical_name,
            "dataset": self.entry.dataset,
            "status": self.entry.status,
            "role": self.entry.role,
            "contract": self.entry.contract,
            "profile": self.profile,
            "path": str(self.path),
            "path_kind": self.path_kind,
            "exists": self.exists,
            "byte_size": self.byte_size,
            "expected_sha256": self.entry.expected_sha256,
            "sha256": self.sha256,
            "checksum_status": self.checksum_status,
        }


class SourceInventory:
    def __init__(
        self,
        *,
        profiles: dict[str, SourceProfile],
        sources: tuple[SourceEntry, ...],
        default_profile: str,
        path: Path,
    ):
        self.profiles = profiles
        self.sources = sources
        self.default_profile = default_profile
        self.path = path
        self._by_key = {source.key: source for source in sources}

    @classmethod
    def load(cls, path: Path | None = None) -> "SourceInventory":
        inventory_path = (path or INVENTORY_PATH).resolve()
        data = tomllib.loads(inventory_path.read_text())
        if data.get("schema_version") not in (1, 2):
            raise ValueError("Unsupported source inventory schema version")
        profiles = {
            name: SourceProfile(
                name=name,
                description=str(value.get("description", "")),
                variables={key: str(item) for key, item in value.get("variables", {}).items()},
            )
            for name, value in data.get("profiles", {}).items()
        }
        sources = tuple(
            SourceEntry(
                key=str(item["key"]),
                logical_name=str(item["logical_name"]),
                dataset=str(item["dataset"]),
                status=str(item["status"]),
                role=str(item["role"]),
                contract=str(item["contract"]),
                path_template=str(item["path"]),
                expected_sha256=item.get("expected_sha256"),
            )
            for item in data.get("sources", [])
        )
        duplicate_keys = len({source.key for source in sources}) != len(sources)
        if duplicate_keys:
            raise ValueError("Source inventory keys must be unique")
        invalid = sorted({source.status for source in sources} - VALID_STATUSES)
        if invalid:
            raise ValueError(f"Invalid source inventory statuses: {invalid}")
        default = str(data.get("default_profile", "mac-local"))
        if default not in profiles:
            raise ValueError(f"Unknown default source profile: {default}")
        return cls(profiles=profiles, sources=sources, default_profile=default, path=inventory_path)

    def profile_variables(
        self, profile: str | None = None, *, source_root: str | Path | None = None
    ) -> tuple[str, dict[str, str]]:
        selected = profile or os.environ.get("UBEM_SOURCE_PROFILE") or self.default_profile
        if selected not in self.profiles:
            raise KeyError(f"Unknown source profile {selected!r}; choose {sorted(self.profiles)}")
        variables = dict(self.profiles[selected].variables)
        root = source_root if source_root is not None else os.environ.get("UBEM_SOURCE_ROOT") or DEFAULT_SOURCE_ROOT
        variables["source_root"] = str(Path(root).expanduser().resolve())
        variables["ubem_repo"] = str(INVENTORY_PATH.parent.parent)
        return selected, variables

    def path_for(
        self,
        key: str,
        *,
        profile: str | None = None,
        source_root: str | Path | None = None,
    ) -> Path:
        try:
            entry = self._by_key[key]
        except KeyError as error:
            raise KeyError(f"Unknown source inventory key: {key}") from error
        _selected, variables = self.profile_variables(profile, source_root=source_root)
        try:
            expanded = string.Template(entry.path_template).substitute(variables)
        except KeyError as error:
            raise ValueError(
                f"Source {key!r} needs undefined profile variable {error.args[0]!r}"
            ) from error
        # Do not resolve paths belonging to another host: macOS can rewrite an
        # absent Linux `/home/...` path through `/System/Volumes/Data`, which
        # would corrupt the portable inventory string.
        return Path(expanded).expanduser()

    def resolve(
        self,
        *,
        profile: str | None = None,
        source_root: str | Path | None = None,
        verify: bool = False,
        statuses: Iterable[str] | None = None,
    ) -> list[ResolvedSource]:
        selected, _variables = self.profile_variables(profile, source_root=source_root)
        allowed = set(statuses) if statuses is not None else None
        resolved: list[ResolvedSource] = []
        for entry in self.sources:
            if allowed is not None and entry.status not in allowed:
                continue
            path = self.path_for(entry.key, profile=selected, source_root=source_root)
            exists = path.exists()
            path_kind = "file" if path.is_file() else "directory" if path.is_dir() else "missing"
            byte_size = path.stat().st_size if path.is_file() else None
            actual = sha256_file(path) if verify and path.is_file() else None
            if not exists:
                checksum_status = "missing"
            elif not verify:
                checksum_status = "not_checked"
            elif not entry.expected_sha256:
                checksum_status = "computed"
            elif actual == entry.expected_sha256:
                checksum_status = "match"
            else:
                checksum_status = "mismatch"
            resolved.append(
                ResolvedSource(
                    entry=entry,
                    profile=selected,
                    path=path,
                    path_kind=path_kind,
                    exists=exists,
                    byte_size=byte_size,
                    sha256=actual,
                    checksum_status=checksum_status,
                )
            )
        return resolved

    def validate(
        self,
        *,
        profile: str | None = None,
        source_root: str | Path | None = None,
        verify: bool = False,
    ) -> dict[str, Any]:
        rows = self.resolve(profile=profile, source_root=source_root, verify=verify)
        bundle_verification = None
        if verify:
            root = Path(source_root or os.environ.get("UBEM_SOURCE_ROOT") or DEFAULT_SOURCE_ROOT)
            if (root / "manifests/file-inventory.json").is_file():
                from .bundle import verify_bundle
                bundle_verification = verify_bundle(root)
        missing_required = [
            row.entry.key for row in rows if row.entry.status == "required" and not row.exists
        ]
        mismatches = [row.entry.key for row in rows if row.checksum_status == "mismatch"]
        return {
            "profile": rows[0].profile if rows else profile or self.default_profile,
            "inventory": str(self.path),
            "valid": not missing_required and not mismatches,
            "missing_required": missing_required,
            "checksum_mismatches": mismatches,
            "bundle_verification": bundle_verification,
            "counts": {
                "entries": len(rows),
                "present": sum(row.exists for row in rows),
                "missing": sum(not row.exists for row in rows),
                "required": sum(row.entry.status == "required" for row in rows),
            },
        }

    def markdown_path_appendix(self) -> str:
        """Render the canonical path appendix used by the operations manual."""
        profiles = ("mac-local", "tower", "linux-server")
        lines = [
            "| Key | Status | Contract | mac-local | tower | linux-server |",
            "|---|---|---|---|---|---|",
        ]
        for source in self.sources:
            values = [str(self.path_for(source.key, profile=profile)) for profile in profiles]
            escaped = [value.replace("|", "\\|") for value in values]
            lines.append(
                f"| `{source.key}` | `{source.status}` | `{source.contract}` | "
                f"`{escaped[0]}` | `{escaped[1]}` | `{escaped[2]}` |"
            )
        return "\n".join(lines)


def load_source_inventory(path: Path | None = None) -> SourceInventory:
    return SourceInventory.load(path)
