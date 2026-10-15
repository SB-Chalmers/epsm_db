"""Stream selection exports without duplicating shared quantities per building."""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql
from pyproj import CRS

from sweden_ubem._logging import operation as logged_operation

from .database import _connect
from .hashing import sha256_file
from .version import SCHEMA_VERSION


@logged_operation("database.export")
def export_dataset(url, dataset, building_ids=None, *, format="geopackage", output=None):
    if format not in ("geopackage", "geoparquet", "csv"):
        raise ValueError("Unsupported export format")
    output = Path(output or f"output/exports/{dataset}-{format}")
    if output.exists():
        raise FileExistsError("Choose a new export path")
    ids = sorted(set(map(str, building_ids))) if building_ids is not None else None
    if ids == []:
        raise ValueError("Select at least one building before export")
    stage = output.with_name(output.name + ".staging-" + uuid4().hex)
    stage.mkdir(parents=True)
    tables = {}
    files = []
    try:
        with _connect(url) as c:
            selection = "SELECT building_id FROM mart.building_facts WHERE dataset_id=%s"
            params = [dataset]
            if ids is None:
                selection += " AND selection_role='primary' AND status_verified AND is_existing"
            else:
                selection += " AND building_id=ANY(%s::uuid[])"
                params.append(ids)
            count = c.execute("SELECT count(*) FROM (" + selection + ") s", params).fetchone()[0]
            if not count or ids is not None and count != len(ids):
                raise ValueError("Selection contains unknown building UUIDs or is empty")
            prefix = (
                "WITH selected AS ("
                + selection
                + "), certificates AS (SELECT DISTINCT certificate_id FROM mart.building_epc WHERE dataset_id=%s AND building_id IN (SELECT building_id FROM selected)) "
            )
            base_params = [*params, dataset]
            specs = []
            for name in (
                "building_facts",
                "building_components",
                "component_heights",
                "building_addresses",
                "building_properties",
                "building_epc",
                "building_areas",
                "scope_completeness",
                "quality_findings",
                "building_purposes",
            ):
                specs.append((name, "building_id IN (SELECT building_id FROM selected)", None))
            specs.append(
                (
                    "building_context",
                    "building_id IN (SELECT building_id FROM selected)",
                    [
                        "dataset_id",
                        "building_id",
                        "area_type",
                        "area_code",
                        "observation_id",
                        "is_primary",
                        "assignment_status",
                    ],
                )
            )
            for name in (
                "epc_certificates",
                "epc_subject_results",
                "epc_candidates",
                "epc_observations",
                "epc_subjects",
            ):
                specs.append(
                    (name, "certificate_id IN (SELECT certificate_id FROM certificates)", None)
                )
            area_condition = "EXISTS(SELECT 1 FROM mart.building_areas r WHERE r.dataset_id=v.dataset_id AND r.area_type=v.area_type AND r.area_code=v.area_code AND r.building_id IN (SELECT building_id FROM selected))"
            specs.extend(
                [
                    ("context_areas", area_condition, None),
                    ("context_observations", area_condition, None),
                    ("source_inventory", "true", None),
                ]
            )
            for name in ("property_units", "property_designations", "property_parts"):
                specs.append(
                    (
                        name,
                        "property_unit_id IN (SELECT property_unit_id FROM mart.building_properties WHERE dataset_id=%s AND building_id IN (SELECT building_id FROM selected))",
                        None,
                    )
                )
            specs.append(
                (
                    "epc_relation_references",
                    "certificate_id IN (SELECT certificate_id FROM certificates)",
                    None,
                )
            )
            for name, where, explicit in specs:
                reference = name == "epc_relation_references"
                view = "building_epc" if reference else name
                if reference:
                    columns = [
                        "dataset_id",
                        "building_id",
                        "subject_id",
                        "certificate_id",
                        "property_unit_id",
                        "evidence_tier",
                    ]
                else:
                    columns = explicit or [
                        r[0]
                        for r in c.execute(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                            ("mart", view),
                        )
                    ]
                expressions = [
                    sql.SQL("ST_AsBinary(v.geom) AS geometry")
                    if k == "geom"
                    else sql.SQL("v.{}").format(sql.Identifier(k))
                    for k in columns
                ]
                if reference:
                    expressions.append(
                        sql.SQL(
                            "v.building_id IN (SELECT building_id FROM selected) AS building_exported"
                        )
                    )
                statement = (
                    sql.SQL(prefix)
                    + sql.SQL("SELECT {} FROM mart.{} v WHERE v.dataset_id=%s AND ").format(
                        sql.SQL(",").join(expressions), sql.Identifier(view)
                    )
                    + sql.SQL(where)
                )
                query_params = [*base_params, dataset] + (
                    [dataset] if view.startswith("property_") else []
                )
                with c.cursor(name="export_" + uuid4().hex) as cursor:
                    cursor.execute(statement, query_params)
                    descriptions = cursor.description
                    fields = [d.name for d in descriptions]
                    geometry = "geometry" in fields
                    arrow_types = {
                        16: pa.bool_(),
                        20: pa.int64(),
                        21: pa.int16(),
                        23: pa.int32(),
                        700: pa.float32(),
                        701: pa.float64(),
                        1082: pa.date32(),
                        1114: pa.timestamp("us"),
                        1184: pa.timestamp("us", tz="UTC"),
                    }
                    schema = pa.schema(
                        [
                            pa.field(
                                d.name,
                                pa.binary()
                                if d.name == "geometry"
                                else arrow_types.get(d.type_code, pa.string()),
                            )
                            for d in descriptions
                        ]
                    )
                    if geometry:
                        schema = schema.with_metadata(
                            {
                                b"geo": json.dumps(
                                    {
                                        "version": "1.0.0",
                                        "primary_column": "geometry",
                                        "columns": {
                                            "geometry": {
                                                "encoding": "WKB",
                                                "crs": CRS(3006).to_json_dict(),
                                                "geometry_types": [],
                                            }
                                        },
                                    }
                                ).encode()
                            }
                        )
                    path = stage / (
                        "evidence.gpkg"
                        if format == "geopackage"
                        else f"{name}.parquet"
                        if format == "geoparquet"
                        else f"{name}.csv"
                    )
                    writer = None
                    stream = None
                    written = 0
                    if format == "geoparquet":
                        writer = pq.ParquetWriter(path, schema)
                    elif format == "csv":
                        stream = path.open("w", newline="", encoding="utf-8")
                        writer = csv.writer(stream)
                        writer.writerow(fields)
                    try:
                        while batch := cursor.fetchmany(8192):
                            data = []
                            for values in batch:
                                record = {}
                                for field, value, field_type in zip(fields, values, schema):
                                    if value is not None:
                                        if field == "geometry":
                                            value = bytes(value)
                                        elif pa.types.is_string(field_type.type):
                                            value = (
                                                json.dumps(value, ensure_ascii=False, default=str)
                                                if isinstance(value, (dict, list, tuple))
                                                else str(value)
                                            )
                                    record[field] = value
                                data.append(record)
                            if format == "geoparquet":
                                writer.write_table(pa.Table.from_pylist(data, schema=schema))
                            elif format == "csv":
                                if geometry:
                                    import shapely

                                    for row in data:
                                        row["geometry"] = (
                                            shapely.from_wkb(row["geometry"]).wkt
                                            if row["geometry"]
                                            else None
                                        )
                                writer.writerows([row[k] for k in fields] for row in data)
                            else:
                                frame = pd.DataFrame(data, columns=fields)
                                if geometry:
                                    geoms = gpd.GeoSeries.from_wkb(frame.pop("geometry"), crs=3006)
                                    gpd.GeoDataFrame(frame, geometry=geoms, crs=3006).to_file(
                                        path,
                                        layer=name,
                                        driver="GPKG",
                                        engine="pyogrio",
                                        mode="a" if written else "w",
                                        geometry_type="Unknown",
                                    )
                                else:
                                    with sqlite3.connect(path) as db:
                                        frame.to_sql(
                                            name,
                                            db,
                                            if_exists="append" if written else "replace",
                                            index=False,
                                        )
                            written += len(data)
                        if format == "geopackage" and (not geometry or not written):
                            with sqlite3.connect(path) as db:
                                if not written:
                                    pd.DataFrame(columns=fields).to_sql(
                                        name, db, index=False, if_exists="replace"
                                    )
                                if db.execute(
                                    "SELECT 1 FROM sqlite_master WHERE name='gpkg_contents'"
                                ).fetchone():
                                    db.execute(
                                        "INSERT OR IGNORE INTO gpkg_contents(table_name,data_type,identifier,description,srs_id) VALUES(?,'attributes',?,'Related evidence table',NULL)",
                                        (name, name),
                                    )
                    finally:
                        if format == "geoparquet":
                            writer.close()
                        if stream:
                            stream.close()
                    tables[name] = written
                    if path not in files:
                        files.append(path)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "dataset": dataset,
            "building_ids": ids,
            "selection": "explicit" if ids else "all_primary_existing",
            "format": format,
            "crs": "EPSG:3006",
            "tables": tables,
            "certificate_count_scope": "loaded_relations",
            "warnings": [
                "Certificate quantities retain their documented owner; relationships may refer to outside buildings."
            ],
            "files": {p.name: sha256_file(p) for p in files},
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        stage.rename(output)
        return manifest
    except BaseException:
        shutil.rmtree(stage)
        raise
