"""Selection preview and independent local layer loaders. Never acquires data."""

from __future__ import annotations
import json
import math
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path
from uuid import UUID

import pyarrow.parquet as pq
import pyarrow as pa
import pyarrow.compute as pc
import pyogrio
import shapely
from psycopg.types.json import Jsonb
from .components import (
    assemble_building_snapshot,
    normalized_geometry_hash,
)
from .database import _connect
from .hashing import content_hash, sha256_file
from .source_inventory import load_source_inventory
from .linkage import (
    building_property_candidates,
    derive_epc_candidates,
    normalize_designation,
)
from . import epc


class LocalSources:
    def __init__(self, profile=None, root=None):
        self.inventory = load_source_inventory()
        self.profile = profile or self.inventory.default_profile
        from .source_inventory import DEFAULT_SOURCE_ROOT
        import os
        self.root = Path(root or os.environ.get("UBEM_SOURCE_ROOT") or DEFAULT_SOURCE_ROOT).expanduser().resolve()

    def path(self, key):
        return self.inventory.path_for(key, profile=self.profile, source_root=self.root)

    def components(self):
        return self.path("tower_component_register_root")


def clean(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        return clean(value.item())
    if isinstance(value, (list, tuple)):
        return [clean(x) for x in value]
    return str(value)


def batches(path, columns=None):
    for batch in pq.ParquetFile(path).iter_batches(batch_size=8192, columns=columns):
        yield from batch.to_pylist()


def component_rows(sources, ids=None, municipality=None):
    selected_ids = pa.array(sorted(ids)) if ids is not None else None
    for path in sorted((sources.components() / "components").glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
            if ids is not None:
                mask = pc.is_in(batch.column("building_id"), value_set=selected_ids)
            else:
                municipalities = batch.column("municipality_values")
                parents = pc.filter(
                    pc.list_parent_indices(municipalities),
                    pc.equal(pc.list_flatten(municipalities), municipality),
                )
                mask = pc.is_in(pa.array(range(batch.num_rows), type=pa.int64()), value_set=parents)
            for row in batch.filter(mask).to_pylist():
                UUID(row["building_id"])
                row["geometry"] = shapely.from_wkb(row["geometry"])
                if normalized_geometry_hash(row["geometry"]) != row["geometry_hash"]:
                    raise ValueError("Component geometry does not match the pinned register hash")
                yield row


def selected_parquet(path, key, ids):
    values = pa.array(sorted(ids))
    for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
        yield from batch.filter(pc.is_in(batch.column(key), value_set=values)).to_pylist()


def primary_selection(sources, municipality):
    manifest = json.loads((sources.components() / "manifest.json").read_text())
    expected = set(manifest.get("partition_sha256", {}))
    actual = {p.name for p in (sources.components() / "components").glob("*.parquet")}
    if not expected or actual != expected:
        raise ValueError("Component register partitions do not reconcile with the manifest")
    if (
        sum(
            pq.ParquetFile(sources.components() / "components" / name).metadata.num_rows
            for name in actual
        )
        != manifest["component_count"]
    ):
        raise ValueError("Component register row counts do not reconcile with the manifest")
    rows = list(component_rows(sources, municipality=municipality))
    # Include UUIDs with any current LM component assigned to the municipality.
    # Conflicts remain explicit and withhold automatic linkage, never choose an owner.
    ids = {r["building_id"] for r in rows}
    # Retrieve complete components, including components outside the selection tile.
    return list(component_rows(sources, ids=ids)), ids


def selection_expansion(sources, rows, ids, municipality):
    """Complete original accepted certificates only, never recursively new ones."""
    root = sources.path("epc_raw_root")
    if not list(root.glob("*/data/*.txt")):
        return (
            rows,
            ids,
            [],
            {"supporting_buildings": 0, "certificate_expansion": "epc_not_available"},
        )
    rawrows = list(epc.selected_rows(root, municipality))
    subjects = {s["epc_subject_id"]: s for r in rawrows if (s := epc.subject(r))}
    grouped = defaultdict(list)
    for r in rows:
        grouped[r["building_id"]].append(r)
    snapshots = [assemble_building_snapshot(b, rs) for b, rs in grouped.items()]
    for s in snapshots:
        if (
            len({m for r in grouped[s["building_id"]] for m in r.get("municipality_values", [])})
            != 1
        ):
            s.update(linkage_eligible=False, linkage_geometry=None)
    # Apply the same automatic UUID-cohort union as the database geometry loader.
    resolved = [s for s in snapshots if s["linkage_eligible"]]
    if not resolved:
        return (
            rows,
            ids,
            rawrows,
            {
                "supporting_buildings": 0,
                "certificate_expansion": "geometry_unresolved",
                "original_certificates": [],
            },
        )
    from .lm_xml import load_selected_buildings

    locator = sources.path("building_registry_locator")
    with sqlite3.connect(f"file:{locator}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        loc = [
            dict(r)
            for bid in sorted(ids)
            if (
                r := db.execute(
                    "SELECT building_id,source_file,source_record FROM registry_buildings WHERE building_id=?",
                    (bid,),
                ).fetchone()
            )
        ]
    registers = load_selected_buildings(loc, sources.path("lm_building_xml_root"))
    houses = {r["building_id"]: r["house_number"] for r in registers}
    for s in resolved:
        s["house_number"] = houses.get(s["building_id"])
    property_path = sources.path("lm_property_geometry")
    geometry = shapely.union_all([s["display_geometry"] for s in resolved])
    frame = pyogrio.read_dataframe(
        property_path, layer="registerenhetsomradesyta", bbox=geometry.bounds, fid_as_index=True
    )
    parts = []
    designations = []
    for fid, r in frame.iterrows():
        if r.geometry is None or not clean(r.get("registerenhetsreferens")):
            continue
        unit = str(r.registerenhetsreferens)
        label = " ".join(x for x in (clean(r.get("trakt")), clean(r.get("etikett"))) if x)
        parts.append(dict(property_unit_id=unit, geometry=r.geometry))
        designations.append(
            dict(
                property_unit_id=unit,
                municipality_code=str(r.kommunkod),
                designation_normalized=normalize_designation(label),
            )
        )
    relations = derive_epc_candidates(
        resolved, building_property_candidates(resolved, parts), designations, subjects.values()
    )
    original = {
        subjects[r["epc_subject_id"]]["certificate_id"]
        for r in relations
        if r["decision"] == "accepted"
    }
    support_ids = set()
    municipal_parts = {}
    for s in subjects.values():
        if s["certificate_id"] not in original:
            continue
        # Subject municipalities are explicit; only matching property parts are read.
        code = s["municipality_code"]
        label = s["designation_normalized"]
        if not code.isdigit() or len(code) != 4:
            raise ValueError("Invalid EPC municipality")
        if code not in municipal_parts:
            candidates = pyogrio.read_dataframe(
                property_path,
                layer="registerenhetsomradesyta",
                where=f"kommunkod='{code}'",
                fid_as_index=True,
            )
            # SQLite UPPER does not normalize Swedish Unicode or whitespace.
            candidates["_match_label"] = candidates.apply(
                lambda p: normalize_designation(
                    " ".join(v for v in (clean(p.get("trakt")), clean(p.get("etikett"))) if v)
                ),
                axis=1,
            )
            municipal_parts[code] = candidates
        matching = municipal_parts[code][municipal_parts[code]["_match_label"] == label]
        for _, part in matching.iterrows():
            buildings = pyogrio.read_dataframe(
                sources.path("lm_building_geometry_primary"),
                layer="byggnad",
                bbox=part.geometry.bounds,
                columns=["objektidentitet"],
            )
            for _, b in buildings.iterrows():
                if b.geometry is not None and b.geometry.intersection(part.geometry).area > 0:
                    support_ids.add(str(b.objektidentitet))
    support_ids -= ids
    # Complete all components for each touched building, not just bbox fragments.
    support = list(component_rows(sources, ids=support_ids)) if support_ids else []
    if {r["building_id"] for r in support} != support_ids:
        raise ValueError(
            "Supporting building geometry does not resolve in the pinned component register"
        )
    return (
        rows + support,
        ids,
        rawrows,
        {
            "supporting_buildings": len(support_ids),
            "certificate_expansion": "original_certificates_only",
            "original_certificates": sorted(original),
        },
    )


def plan_dataset(sources, municipality="1485", *, prepared=None):
    if not municipality.isdigit() or len(municipality) != 4:
        raise ValueError("Four-digit municipality required")
    if prepared is None:
        rows, ids = primary_selection(sources, municipality)
        rows, ids, rawrows, expansion = selection_expansion(sources, rows, ids, municipality)
    else:
        rows, ids, rawrows, expansion = prepared
    bytes_ = sum(len(r["geometry"].wkb) for r in rows)
    # Conservative allowance for raw/normalized geometries, attributes, indexes, WAL.
    estimate = max(512 * 1024**2, bytes_ * 30 + len(ids) * 65536)
    free = shutil.disk_usage(Path.cwd()).free
    missing = []
    for key in (
        "lm_building_geometry_primary",
        "building_registry_locator",
        "lm_property_geometry",
        "height_staging_catalogue",
        "lm_building_xml_root",
        "lm_address_xml_root",
        "epc_raw_root",
    ):
        if not sources.path(key).exists():
            missing.append(key)
    epc_bytes = sum(p.stat().st_size for p in sources.path("epc_raw_root").glob("*/data/*.txt"))
    municipality_codes = defaultdict(set)
    for r in rows:
        municipality_codes[r["building_id"]].update(r["municipality_values"])
    return {
        "municipality": municipality,
        "primary_buildings": len(ids),
        "components": len(rows),
        "estimated_database_bytes": estimate,
        "temporary_reserve_bytes": estimate,
        "free_bytes": free,
        "disk_ok": free > estimate * 2 + 2 * 1024**3,
        "missing_sources": missing,
        "epc_stream_bytes_per_pass": epc_bytes,
        **expansion,
        "municipality_conflict_buildings": sum(
            len(codes) != 1 for codes in municipality_codes.values()
        ),
        "height_publication_status": "staging_only",
    }


def register_files(url, sources, layer, paths, metadata=None):
    paths = sorted({Path(p).resolve() for p in paths if Path(p).is_file()})
    if not paths:
        raise FileNotFoundError(f"No source files for {layer}")
    fingerprints = {str(p): sha256_file(p) for p in paths}
    # Moving an identical source package to another host does not change identity.
    version = f"{layer}:{content_hash(sorted(set(fingerprints.values())))}"
    with _connect(url) as c:
        publisher = (
            "Lantmäteriet"
            if layer in ("geometry", "register", "addresses", "properties")
            else "Boverket"
            if layer == "epc"
            else "SCB"
            if layer in ("municipality", "population", "regso", "income")
            else "published site evidence"
            if layer == "epc_site_events"
            else "component-height pipeline"
        )
        c.execute(
            "INSERT INTO catalog.source_version(source_version_id,layer,publisher,contract,publication_status,metadata) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                version,
                layer,
                publisher,
                "epc-site-event-v1" if layer == "epc_site_events" else f"{layer}-v001",
                "staging_only" if layer == "heights" else "source_snapshot",
                Jsonb(metadata or {}),
            ),
        )
        for p in paths:
            digest = fingerprints[str(p)]
            from .compact import register_asset
            register_asset(c, version, f"{p.name}@{digest}", p, digest, sources)
    return version, fingerprints


def verify_unchanged(fingerprints):
    for path, digest in fingerprints.items():
        if sha256_file(path) != digest:
            raise ValueError(f"Source changed during projection: {path}")


def measurement_record(path, result, cache):
    """Resolve a catalogue locator, checking the original measurement owner/geometry."""
    if path.suffix != ".parquet":
        return json.loads(path.read_text()), None, "0"
    if path not in cache:
        cache[path] = pq.ParquetFile(path).read()
    table = cache[path]
    offset = result.get("baseline_row_offset")
    if offset is not None:
        offset = int(offset)
        if offset < 0 or offset >= table.num_rows:
            raise ValueError("Height measurement row offset is out of range")
        record = table.slice(offset, 1).to_pylist()[0]
    elif "component_id" in table.column_names:
        matches = pc.indices_nonzero(pc.equal(table.column("component_id"), result["component_id"]))
        if len(matches) != 1:
            raise ValueError("Height measurement component locator is not unique")
        offset = int(matches[0].as_py())
        record = table.slice(offset, 1).to_pylist()[0]
    else:
        raise ValueError("Height measurement requires a component ID or verified row offset")
    if record.get("building_id") != result["building_id"]:
        raise ValueError("Height measurement building locator mismatch")
    geom = shapely.from_wkb(record["geometry"]) if record.get("geometry") else None
    if geom is None or normalized_geometry_hash(geom) != result["input_geometry_hash"]:
        raise ValueError("Height measurement geometry locator mismatch")
    payload = {k: clean(v) for k, v in record.items() if k != "geometry"}
    return payload, geom, str(offset)


def municipality_rows(snapshot, municipality):
    """Dissolve pinned RegSO polygons; never use the simplified municipality map."""
    import geopandas as gpd
    from .scb import validate_scb_snapshot
    from .context import _property

    manifest = validate_scb_snapshot(snapshot)
    if str(manifest["municipality"]) != municipality:
        raise ValueError("Wrong municipality in boundary snapshot")
    request = json.loads((snapshot / manifest["regso"]["request_file"]).read_text())
    if request.get("srsName") != "EPSG:3006":
        raise ValueError("RegSO boundary request must declare EPSG:3006")
    document = json.loads((snapshot / manifest["regso"]["response_file"]).read_text())
    features = document.get("features") or []
    matched = document.get("numberMatched")
    if matched not in (None, "unknown") and int(matched) != len(features):
        raise ValueError("Incomplete RegSO boundary response")
    geometries, codes = [], []
    for feature in features:
        p = feature.get("properties") or {}
        if str(_property(p, "kommunkod") or municipality).zfill(4) != municipality:
            raise ValueError("RegSO boundary contains a different municipality")
        code = _property(p, "regsokod", "RegSO_kod", "regso_code")
        if not code:
            raise ValueError("RegSO boundary lacks an area code")
        codes.append(str(code).replace(f"_RegSO{manifest['regso']['year']}", ""))
        g = shapely.geometry.shape(feature["geometry"])
        if g.is_empty or not g.is_valid or g.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Invalid RegSO boundary geometry")
        geometries.append(g)
    if not geometries or len(set(codes)) != len(codes):
        raise ValueError("Empty or duplicate RegSO boundary set")
    if set(codes) != set(manifest["regso"]["area_codes"]):
        raise ValueError("RegSO boundary codes differ from the pinned manifest")
    frame = gpd.GeoDataFrame(
        {"KnKod": [municipality], "KnNamn": [f"Municipality {municipality}"]},
        geometry=[shapely.union_all(geometries)],
        crs=3006,
    )
    frame.attrs.update(
        boundary_policy="scb-regso-municipality-union-v1",
        snapshot_id=manifest["snapshot_id"],
        reference_period=str(manifest["regso"]["year"]),
        source_area_codes=sorted(codes),
        boundary_kind="union_of_regso",
    )
    return frame


def load_dataset(
    url,
    sources=None,
    *,
    dataset="uddevalla",
    municipality="1485",
    layers=None,
    assessment_date=None,
    epc_events=None,
    source="national",
):
    """Process imported evidence. Source objects are never dereferenced here."""
    if epc_events is not None:
        raise ValueError(
            "Import site events through db init; processing accepts database source versions"
        )
    from .selection import process_dataset

    return process_dataset(
        url,
        dataset=dataset,
        municipality=municipality,
        layers=layers,
        assessment_date=assessment_date,
        source=source,
    )
