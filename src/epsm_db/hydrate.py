"""Batched evidence hydration over GIS reader views; no per-building queries."""

from collections import defaultdict
from copy import deepcopy

from shapely import from_wkb

from .enums import BuildingType
from .epc import observations
from .records import BuildingRecord, Receipt


def _rows(connection, statement, values):
    result = []
    for original in connection.execute(statement, values):
        row = dict(original)
        row.pop("geom", None)
        if "geometry_wkb" in row:
            value = row.pop("geometry_wkb")
            row["geometry"] = from_wkb(bytes(value)) if value is not None else None
        result.append(row)
    return result


def _group(rows, key):
    result = defaultdict(list)
    for row in rows:
        result[row[key]].append(row)
    return result


def hydrate(connection, facts, publication, request, database_url):
    ids = [row["fid"] for row in facts]
    if not ids:
        return []
    components = _group(
        _rows(
            connection,
            """SELECT c.*,c.external_id AS component_external_id,
        ST_AsBinary(c.geom) AS geometry_wkb FROM gis.model_components c
        WHERE building_id=ANY(%s) ORDER BY component_id""",
            (ids,),
        ),
        "building_id",
    )
    heights = _group(
        _rows(
            connection,
            """SELECT h.*,c.building_id FROM gis.height_evidence h
        JOIN gis.model_components c ON c.component_id=h.component_id
        WHERE c.building_id=ANY(%s) ORDER BY h.fid""",
            (ids,),
        ),
        "building_id",
    )
    addresses = _group(
        _rows(
            connection,
            """SELECT l.building_id,l.address_external_id,l.method,
        a.*,ST_AsBinary(a.geom) AS geometry_wkb FROM gis.building_addresses l
        LEFT JOIN gis.model_addresses a ON a.address_id=l.address_id
        WHERE l.building_id=ANY(%s) ORDER BY l.fid""",
            (ids,),
        ),
        "building_id",
    )
    properties = _group(
        _rows(
            connection,
            """SELECT l.building_id,l.status,l.method,l.overlap_fraction,
        p.*,ST_AsBinary(p.geom) AS geometry_wkb FROM gis.model_property_links l
        JOIN gis.model_properties p USING(property_id)
        WHERE l.building_id=ANY(%s) ORDER BY l.link_id""",
            (ids,),
        ),
        "building_id",
    )
    parts = _group(
        _rows(
            connection,
            """SELECT DISTINCT l.building_id,p.*,
        'property_link'::text AS relationship,ST_AsBinary(p.geom) AS geometry_wkb
        FROM gis.model_property_links l JOIN gis.model_property_parts p USING(property_id)
        WHERE l.building_id=ANY(%s)
        UNION ALL
        SELECT b.building_id,p.*,'unresolved_spatial_candidate'::text AS relationship,
        ST_AsBinary(p.geom) AS geometry_wkb FROM gis.model_buildings b
        JOIN gis.model_property_parts p ON p.property_id IS NULL AND p.geom && b.geom
            AND ST_Intersects(p.geom,b.geom)
        WHERE b.building_id=ANY(%s)""",
            (ids, ids),
        ),
        "building_id",
    )
    areas = _group(
        _rows(
            connection,
            "SELECT * FROM gis.building_areas WHERE building_id=ANY(%s) ORDER BY fid",
            (ids,),
        ),
        "building_id",
    )
    links = _group(
        _rows(
            connection,
            "SELECT * FROM gis.model_epc_links WHERE building_id=ANY(%s) ORDER BY link_id",
            (ids,),
        ),
        "building_id",
    )
    cert_ids = sorted({r["epc_id"] for rows in links.values() for r in rows})
    certificates = {
        r["epc_id"]: r
        for r in _rows(
            connection,
            "SELECT * FROM gis.model_certificates WHERE epc_id=ANY(%s) ORDER BY epc_id",
            (cert_ids,),
        )
    }
    subjects = _group(
        _rows(
            connection,
            "SELECT * FROM gis.model_epc_subjects WHERE epc_id=ANY(%s) ORDER BY subject_id",
            (cert_ids,),
        ),
        "epc_id",
    )
    raw = _group(
        _rows(
            connection,
            """SELECT r.*,s.path FROM gis.epc_records r
        LEFT JOIN gis.sources s ON s.fid=r.file_id WHERE r.epc_id=ANY(%s) ORDER BY r.fid""",
            (cert_ids,),
        ),
        "epc_id",
    )
    mappings = [dict(r) for r in connection.execute("SELECT * FROM gis.model_field_mapping")]
    by_certificate = {}
    for cid, certificate in certificates.items():
        certificate["source_records"] = deepcopy(raw[cid])
        by_certificate[cid] = observations(
            [
                {
                    "certificate_id": certificate["certificate_id"],
                    "regime": certificate["regime"],
                    "source_file": r["path"],
                    "source_record": r["source_record"],
                    "original_values": r["attributes"],
                }
                for r in raw[cid]
            ],
            mappings,
        )
        for observation in by_certificate[cid]:
            definition = next(
                (
                    m
                    for m in mappings
                    if m["regime"] == certificate["regime"]
                    and m["source_field"] == observation["source_field"]
                ),
                {},
            )
            observation["mapping_authority"] = definition.get("authority")
    records = []
    for original in facts:
        f = dict(original)
        key, uuid = f["fid"], f["building_id"]
        geometry = f.pop("geometry_wkb", None)
        geometry = from_wkb(bytes(geometry)) if geometry is not None else None
        cert_keys = sorted({r["epc_id"] for r in links[key]})
        certs = tuple(deepcopy(certificates[cid]) for cid in cert_keys)
        receipt = Receipt(
            publication["dataset_id"],
            uuid,
            request=deepcopy(request),
            snapshot=deepcopy(publication),
            identities={"internal_id": key, "source_id": uuid},
        )
        if publication["incomplete_source_files"]:
            receipt.quality.append(
                "This publication contains incomplete source files; coverage is partial."
            )
        evidence = []
        for link in links[key]:
            certificate = certificates[link["epc_id"]]
            temporal = (
                "construction_after_certificate"
                if certificate["certificate_date"]
                and f.get("construction_year")
                and f["construction_year"] > certificate["certificate_date"].year
                else "unverified"
            )
            evidence.append(
                dict(link, certificate_id=certificate["certificate_id"], temporal_status=temporal)
            )
        f["internal_id"] = key
        record = BuildingRecord(
            publication["dataset_id"],
            uuid,
            BuildingType(f["building_type"]),
            geometry,
            f,
            components=tuple(components[key]),
            heights=tuple(heights[key]),
            purposes=tuple(f.get("purposes") or ()),
            addresses=tuple(addresses[key]),
            properties=tuple(properties[key]),
            certificates=certs,
            observations=tuple(o for cid in cert_keys for o in by_certificate[cid]),
            subjects=tuple(
                dict(s, certificate_id=certificates[cid]["certificate_id"])
                for cid in cert_keys
                for s in subjects[cid]
            ),
            epc_results=tuple(evidence),
            receipt=receipt,
            areas=tuple(areas[key]),
            property_parts=tuple(parts[key]),
            _database_url=database_url,
        )
        receipt.fields["footprint"] = {
            "source_state": "available"
            if geometry is not None and geometry.is_valid
            else "missing_or_invalid",
            "origin": "source component union",
            "unit": "m2",
            "value": geometry.area if geometry is not None else None,
        }
        receipt.fields["construction_year"] = {
            "source_state": "reported" if f.get("construction_year") else "missing",
            "value": f.get("construction_year"),
        }
        receipt.fields["building_type"] = {
            "source_state": "inferred",
            "value": record.type.value,
            "origin": "registry purposes / scope-qualified EPC programme",
        }
        for name, source, integer in [
            ("atemp_m2", "EgenAtemp", False),
            ("storeys", "EgenAntalPlan", True),
            ("basement_storeys", "EgenAntalKallarplan", True),
        ]:
            value, state = record.observation(source, integer=integer)
            usable = record.single_building_epc and value is not None
            if not record.single_building_epc:
                state = "unresolved_certificate_scope"
            elif value is not None and (value < 0 or name != "basement_storeys" and value == 0):
                state, usable = "suspect", False
                receipt.quality.append(f"Invalid reported {name}")
            receipt.fields[name] = {
                "source_state": state,
                "value": value,
                "usable_for_building": usable,
                "origin": "reported certificate value",
            }
        receipt.fields["height_components"] = {
            "source_state": "available" if record.heights else "missing",
            "value": len(record.heights),
            "expected_components": len(record.components),
            "origin": "component identity; all height evidence retained",
        }
        if certs and not record.single_building_epc:
            receipt.quality.append(
                "No unique latest single-building EPC; certificate quantities are not allocated."
            )
        receipt.quality.extend(record.temporal_warnings)
        records.append(record)
    return records
