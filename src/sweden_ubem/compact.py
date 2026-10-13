"""Writes to compact physical relations behind stable consumer projections."""
from collections import defaultdict
from pathlib import Path

from psycopg.types.json import Jsonb


def register_asset(c, version, logical, path, digest, sources):
    import os
    import pyarrow.parquet as pq
    path = Path(path).resolve()
    root = getattr(sources, "root", None)
    if root is None and hasattr(sources, "paths"):
        root = Path(os.path.commonpath([str(p) for p in sources.paths.values()]))
    if root is None:
        raise ValueError("A portable source root is required for asset registration")
    root = Path(root).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Source asset is outside the configured bundle: {path}")
    size = path.stat()
    c.execute("INSERT INTO catalog.asset(sha256,byte_size) VALUES(%s,%s) ON CONFLICT DO NOTHING", (digest, size.st_size))
    asset, recorded_size = c.execute("SELECT asset_key,byte_size FROM catalog.asset WHERE sha256=%s", (digest,)).fetchone()
    if recorded_size != size.st_size:
        raise ValueError("Registered asset byte size changed")
    c.execute("INSERT INTO catalog.asset_location_data(asset_key,profile,path,mtime_ns) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (asset, sources.profile, str(path.relative_to(root)), size.st_mtime_ns))
    format = path.suffix.lower().lstrip(".")
    metadata = {"columns": pq.read_schema(path).names} if format == "parquet" and logical == "catalogue.parquet" else {}
    c.execute("INSERT INTO catalog.source_file_data(source_version_id,logical_name,asset_key,bundle_path,display_path,format,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
              (version, logical, asset, str(path.relative_to(root)), str(path), format, Jsonb(metadata)))
    row = c.execute("SELECT occurrence_key,asset_key,bundle_path FROM catalog.source_file_data WHERE source_version_id=%s AND logical_name=%s", (version, logical)).fetchone()
    if row[1:] != (asset, str(path.relative_to(root))):
        raise ValueError("Logical source occurrence conflicts with registered asset")
    return row[0]


def component_keys(c, dataset, identifiers):
    identifiers = set(identifiers)
    if not identifiers:
        return {}
    values = [{"building_id": value.split(":", 1)[0], "hash": value.split(":", 1)[1]}
              for value in identifiers]
    result = dict(c.execute(
        "SELECT d.building_id::text||':'||encode(d.geometry_hash,'hex'),d.component_key "
        "FROM stock.component_data d JOIN jsonb_to_recordset(%s) v(building_id uuid,hash text) "
        "ON (d.building_id,d.geometry_hash)=(v.building_id,decode(v.hash,'hex')) WHERE d.dataset_id=%s",
        (Jsonb(values), dataset)))
    if set(result) != identifiers:
        raise ValueError("Component reference is not registered in this source snapshot")
    return result


def locate(c, rows):
    from .jobs import bulk_insert
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["dataset_id"], row["source_version_id"], row["source_file"],
                 row["source_sha256"], row["kind"], row.get("_logical_name"))].append(row)
    requests = [dict(request=n,version=k[1],path=k[2],digest=k[3],logical=k[5])
                for n,k in enumerate(grouped)]
    found = defaultdict(list)
    for request,occurrence in c.execute(
        "SELECT v.request,f.occurrence_key FROM jsonb_to_recordset(%s) "
        "v(request integer,version text,path text,digest text,logical text) "
        "JOIN catalog.source_file_data f ON f.source_version_id=v.version "
        "JOIN catalog.asset a ON a.asset_key=f.asset_key AND a.sha256=v.digest "
        "WHERE (v.logical IS NOT NULL AND f.logical_name=v.logical) "
        "OR (v.logical IS NULL AND f.display_path=v.path)",(Jsonb(requests),)):
        found[request].append(occurrence)
    locators, identities = [], {}
    for n,((dataset,version,path,digest,kind,logical),values) in enumerate(grouped.items()):
        if len(found[n]) != 1:
            raise ValueError(f"Source occurrence is missing or ambiguous: {path}")
        occurrence = found[n][0]
        for row in values:
            locator=dict(dataset_id=dataset,occurrence_key=occurrence,kind=kind,
                         source_record=row["source_record"],source_member=row.get("source_member"))
            key=(dataset,occurrence,kind,row["source_record"],row.get("source_member"))
            identities[key]=(version,path,kind,row["source_record"])
            locators.append(locator)
    bulk_insert(c,"catalog.record_locator",locators)
    result = {}
    for dataset,occurrence,kind,record,member,key in c.execute(
        "SELECT r.dataset_id,r.occurrence_key,r.kind,r.source_record,r.source_member,r.record_key "
        "FROM catalog.record_locator r JOIN jsonb_to_recordset(%s) "
        "v(dataset_id text,occurrence_key bigint,kind text,source_record text,source_member text) "
        "ON r.dataset_id=v.dataset_id AND r.occurrence_key=v.occurrence_key AND r.kind=v.kind "
        "AND r.source_record=v.source_record AND r.source_member IS NOT DISTINCT FROM v.source_member",
        (Jsonb(locators),)):
        result[identities[(dataset,occurrence,kind,record,member)]]=key
    if len(result)!=len(identities):
        raise ValueError("Record locator registration did not reconcile")
    return result


def project_write(c, table, rows, conflict):
    """Return a physical write, or consume a compound locator/evidence write."""
    from .jobs import bulk_insert
    if table == "stock.geometry_component":
        return "stock.component_data", [dict(
            **{k: v for k, v in row.items() if k not in ("component_id", "geometry_hash")},
            geometry_hash=bytes.fromhex(row["geometry_hash"])) for row in rows], conflict
    if table in {"stock.component_source_feature", "stock.component_relation", "evidence.component_height"}:
        groups = defaultdict(list)
        for row in rows:
            groups[row["dataset_id"]].append(row)
        converted = []
        for dataset, values in groups.items():
            refs = [r["component_id"] for r in values] if table != "stock.component_relation" else [r[k] for r in values for k in ("left_component_id", "right_component_id")]
            keys = component_keys(c, dataset, refs)
            lineage_occurrences = {}
            if table == "stock.component_source_feature":
                lineage_occurrences = dict(c.execute("SELECT source_version_id,occurrence_key FROM catalog.source_file_data WHERE source_version_id=ANY(%s) AND logical_name='source-lineage.parquet'", (list({r["source_version_id"] for r in values}),)))
            for row in values:
                if table == "stock.component_source_feature":
                    occurrence = lineage_occurrences.get(row["source_version_id"])
                    if not occurrence:
                        raise ValueError("Component lineage occurrence is not registered")
                    payload = row["original_values"]
                    converted.append(dict(component_key=keys[row["component_id"]], source_fid=row["source_fid"],
                                          occurrence_key=occurrence, disposition=row["disposition"],
                                          **{k: payload.get(k) for k in ("revision_ts", "object_version", "outline", "municipality", "house_number")}))
                elif table == "stock.component_relation":
                    converted.append(dict(**{k: v for k, v in row.items() if k not in ("left_component_id", "right_component_id")},
                                          left_component_key=keys[row["left_component_id"]], right_component_key=keys[row["right_component_id"]]))
                else:
                    if row["input_geometry_hash"] != row["component_id"].split(":", 1)[1]:
                        raise ValueError("Height geometry hash differs from component identity")
                    asset = None
                    if row.get("evidence_sha256"):
                        asset = c.execute("SELECT asset_key FROM catalog.asset WHERE sha256=%s", (row["evidence_sha256"],)).fetchone()[0]
                    converted.append(dict(**{k: v for k, v in row.items() if k not in ("dataset_id", "component_id", "input_geometry_hash", "evidence_sha256")},
                                          component_key=keys[row["component_id"]], evidence_asset_key=asset))
        target = {"stock.component_source_feature": "stock.component_lineage", "stock.component_relation": "stock.component_relation_data",
                  "evidence.component_height": "evidence.component_height_data"}[table]
        return target, converted, conflict
    if table == "evidence.epc_subject":
        representatives = {}
        for dataset in {r["dataset_id"] for r in rows}:
            subjects = [r["subject_id"] for r in rows if r["dataset_id"] == dataset]
            for subject, key in c.execute(
                "SELECT r.subject_id,min(r.record_key) FROM (SELECT record_key,subject_id FROM raw_epc.before_v260_data WHERE subject_id=ANY(%s) UNION ALL "
                "SELECT record_key,subject_id FROM raw_epc.after_v260_data WHERE subject_id=ANY(%s)) r JOIN catalog.record_locator l USING(record_key) "
                "WHERE l.dataset_id=%s GROUP BY r.subject_id", (subjects, subjects, dataset)):
                representatives[(dataset, subject)] = key
        converted = []
        for row in rows:
            key = representatives.get((row["dataset_id"], row["subject_id"]))
            if key is None:
                raise ValueError("EPC representative source row is not retained")
            converted.append(dict(**{k: v for k, v in row.items() if k != "original_values"},
                                  representative_record_key=key))
        return "evidence.epc_subject_data", converted, conflict
    if table not in {"raw_lm.record", "raw_height.record", "raw_scb.record", "raw_epc.before_v260_row", "raw_epc.after_v260_row"}:
        return table, rows, conflict
    keys = locate(c, rows)
    converted = []
    properties = {}
    components = {}
    measurements = {}
    if table == "raw_lm.record":
        for dataset in {r["dataset_id"] for r in rows}:
            parts = [r["original_values"].get("objektidentitet") for r in rows if r["dataset_id"] == dataset and r["kind"] == "property"]
            if parts:
                for part, geometry in c.execute("SELECT property_area_part_id,ST_AsBinary(geom) FROM stock.property_area_part WHERE dataset_id=%s AND property_area_part_id=ANY(%s)", (dataset, parts)):
                    properties[(dataset, part)] = bytes(geometry)
    if table == "raw_height.record":
        for dataset in {r["dataset_id"] for r in rows}:
            ids = [r["original_values"]["component_id"] for r in rows if r["dataset_id"] == dataset and r["kind"] == "component_height"]
            components.update({(dataset, key): value for key, value in component_keys(c, dataset, ids).items()})
        lookups = [{"version": r["source_version_id"], "root": r["original_values"].get("evidence_root"), "path": r["original_values"]["evidence_record_path"]}
                   for r in rows if r["kind"] == "component_height" and r["original_values"].get("evidence_record_path")]
        for version, root, path, key in c.execute("SELECT DISTINCT m.source_version_id,m.evidence_root,m.original_path,m.measurement_key FROM evidence.measurement_source m JOIN jsonb_to_recordset(%s) v(version text,root text,path text) ON m.source_version_id=v.version AND m.evidence_root IS NOT DISTINCT FROM v.root AND m.original_path=v.path", (Jsonb(lookups),)):
            measurements[(version, root, path)] = key
    for row in rows:
        key = keys[(row["source_version_id"], row["source_file"], row["kind"], row["source_record"])]
        payload = row["original_values"]
        if table.startswith("raw_epc."):
            converted.append(dict(record_key=key, original_values=payload,
                                  certificate_id=row.get("certificate_id"), subject_id=row.get("subject_id")))
        elif table == "raw_scb.record":
            converted.append(dict(record_key=key, original_values=payload, original_geometry=row.get("original_geometry")))
        elif table == "raw_lm.record" and row["kind"] == "property":
            part = payload.get("objektidentitet")
            unit = payload.get("registerenhetsreferens")
            existing = properties.get((row["dataset_id"], part))
            geometry = row.get("original_geometry")
            sparse = geometry if existing != geometry else None
            converted.append(dict(record_key=key, property_part_id=part, property_unit_id=unit, diagnostic_geometry=sparse))
        elif table == "raw_height.record" and row["kind"] == "component_height":
            component = components[(row["dataset_id"], payload["component_id"])]
            measurement = None
            if payload.get("evidence_record_path"):
                measurement = measurements.get((row["source_version_id"], payload.get("evidence_root"), payload["evidence_record_path"]))
                if measurement is None:
                    raise ValueError("Height measurement is outside the verified source closure")
            reserved = {"building_id", "component_id", "input_geometry_hash", "height_status", "representative_height_m", "eave_height_m", "ridge_height_m", "roof_form", "evidence_record_path", "evidence_record_sha256", "evidence_root", "baseline_row_offset", "owner_tile_id", "geometry_status", "failure_reason", "processing_status", "processing_error"}
            converted.append(dict(component_key=component, record_key=key, measurement_key=measurement,
                                  baseline_row_offset=int(payload["baseline_row_offset"]) if payload.get("baseline_row_offset") is not None else None,
                                  **{k: payload.get(k) for k in ("owner_tile_id", "geometry_status", "failure_reason", "processing_status", "processing_error")},
                                  extra_fields={**{k: v for k, v in payload.items() if k not in reserved},
                                      **({"height_status": payload.get("height_status")} if not payload.get("height_status") else {}),
                                      **({"evidence_root": payload.get("evidence_root")} if not payload.get("evidence_record_path") else {})}))
    if table == "raw_lm.record":
        if converted:
            bulk_insert(c, "raw_lm.property_diagnostic", converted)
        return None, None, len(rows)
    if table == "raw_height.record":
        if converted:
            bulk_insert(c, "evidence.height_catalogue", converted)
        return None, None, len(rows)
    target = {"raw_scb.record": "raw_scb.record_data", "raw_epc.before_v260_row": "raw_epc.before_v260_data",
              "raw_epc.after_v260_row": "raw_epc.after_v260_data"}[table]
    from .jobs import _worker
    worker = _worker.get()
    if table.startswith("raw_epc.") and worker and worker["connection"] is c and worker["active"] and all(r.get("_logical_name") for r in rows):
        # Source-position fencing makes these new row IDs append-only. Metadata/subjects
        # still merge, while each original EPC row is copied directly once.
        conflict = "append"
    return target, converted, conflict
