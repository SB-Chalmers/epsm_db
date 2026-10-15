"""One-time national source import. Processing never calls this module's readers."""

import inspect
import json
import os
import shutil
import sqlite3
from collections import defaultdict
from itertools import islice
from pathlib import Path
from uuid import UUID

import pyarrow.parquet as pq
import pyogrio
import shapely
from psycopg.types.json import Jsonb

from sweden_ubem._logging import operation as logged_operation

from . import epc
from .components import normalized_geometry_hash
from .database import _connect
from .dataset import clean, municipality_rows
from .epc_assessment import lm_address
from .hashing import content_hash, sha256_file
from .jobs import BatchTooLarge, _worker, batches, bulk_insert, serial_worker, work
from .linkage import normalize_designation
from .lm_xml import _selected_elements, parse_address_element, parse_building_element
from .progress import report as progress_report

SOURCE_LAYERS = (
    "geometry",
    "register",
    "addresses",
    "properties",
    "heights",
    "epc",
    "population",
    "municipality",
    "regso",
    "income",
)


def arrow_rows(path, layer, *, skip_features=0):
    with pyogrio.open_arrow(
        path,
        layer=layer,
        return_fids=True,
        use_pyarrow=True,
        batch_size=8192,
        datetime_as_string=True,
        skip_features=skip_features,
    ) as (meta, reader):
        if meta.get("crs") != "EPSG:3006":
            raise ValueError(f"Expected EPSG:3006: {path}")
        geometry = meta.get("geometry_name") or "wkb_geometry"
        fid = meta["fid_column"]
        position=skip_features
        for batch in reader:
            for row in batch.to_pylist():
                row["_source_position"]=position
                position+=1
                row["_fid"] = int(row.pop(fid))
                row["geometry"] = shapely.from_wkb(row.pop(geometry)) if row.get(geometry) else None
                yield row


def parquet_rows(path, skip=0, columns=None):
    """Skip committed row groups, keeping decoded pages bounded."""
    file = pq.ParquetFile(path)
    start = 0
    for group in range(file.num_row_groups):
        count = file.metadata.row_group(group).num_rows
        if start + count <= skip:
            start += count
            continue
        position = start
        for batch in file.iter_batches(batch_size=8192, row_groups=[group],columns=columns):
            for row in batch.to_pylist():
                if position >= skip:
                    yield dict(row, _source_position=position)
                position += 1
        start += count


def gpkg_fids(path, layer, skip=0):
    """Register originals by actual FID without duplicating or decoding their geometry."""
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as c:
        srid = c.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?", (layer,)).fetchone()
        if srid != (3006,):
            raise ValueError(f"Expected EPSG:3006: {path}")
        columns = c.execute('PRAGMA table_info("' + layer.replace('"', '""') + '")').fetchall()
        primary = [r[1] for r in columns if r[5]]
        if len(primary) != 1:
            raise ValueError("Source feature primary key is ambiguous")
        column = '"' + primary[0].replace('"', '""') + '"'
        table = '"' + layer.replace('"', '""') + '"'
        cursor = c.execute(f"SELECT {column} FROM {table} ORDER BY {column} LIMIT -1 OFFSET ?", (skip,))
        while batch := cursor.fetchmany(8192):
            for (fid,) in batch:
                yield {"_fid": fid}


class Importer:
    def __init__(self, url, sources, source="national", bundle=None, batch_rows=None, batch_bytes=None):
        self.url, self.sources, self.source = url, sources, source
        self.bundle = bundle or {}
        with _connect(url) as c:
            self.digest = c.execute(
                "SELECT metadata->>'projection_digest' FROM catalog.dataset WHERE dataset_id=%s",
                (source,),
            ).fetchone()[0]
            self.completed = defaultdict(dict)
            self.completed_files = {}
            self.batch_rows = {}
            for stage, partition, signature, checkpoint in c.execute(
                "SELECT stage,partition_key,input_signature,checkpoint FROM catalog.work_unit "
                "WHERE owner_dataset_id=%s AND processing_release_id IS NULL "
                "AND selection_dataset_id IS NULL AND state='complete' AND stage LIKE 'import:%%'",
                (source,),
            ):
                key, separator, ordinal = partition.rpartition(":")
                layer = stage.removeprefix("import:")
                if separator and ordinal == "EOF":
                    self.completed_files[(layer, key)] = signature
                if separator and ordinal.isdecimal():
                    self.completed[(layer, key)][partition] = signature
                    self.batch_rows[(layer, partition)] = checkpoint.get("source_rows")
        self.max_rows = batch_rows if batch_rows is not None else int(os.environ.get("UBEM_BATCH_ROWS", "8192"))
        self.max_bytes = batch_bytes if batch_bytes is not None else int(os.environ.get("UBEM_BATCH_BYTES", str(64 * 1024**2)))
        if self.max_rows < 1 or self.max_bytes < 1:
            raise ValueError("Batch limits must be positive")
        self.version = None
        self.fingerprints = {}
        self.logical = {}

    def register(self, layer, files, metadata=None):
        entries = []
        for logical, path in files:
            path = Path(path).resolve()
            if not path.is_file():
                raise FileNotFoundError(path)
            digest = sha256_file(path)
            self.fingerprints[str(path)] = digest
            self.logical[str(path)] = logical
            entries.append([logical, digest, path.stat().st_size])
        version = f"{layer}:{content_hash(sorted(entries))}"
        with _connect(self.url) as c:
            publisher = (
                "Lantmäteriet"
                if layer in ("geometry", "register", "addresses", "properties")
                else "Boverket"
                if layer == "epc"
                else "SCB"
                if layer in ("population", "municipality", "regso", "income")
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
                    f"{layer}-national-v1",
                    "staging_only" if layer == "heights" else "source_snapshot",
                    Jsonb(metadata or {}),
                ),
            )
            for logical, path in files:
                path = Path(path).resolve()
                self.asset(c, version, logical, path, self.fingerprints[str(path)])
        self.version = version
        return version

    def asset(self, c, version, logical, path, digest):
        from .compact import register_asset
        return register_asset(c, version, logical, path, digest, self.sources)

    def raw(self, kind, path, ordinal, values, geometry=None, **extra):
        path = str(Path(path).resolve())
        return dict(
            dataset_id=self.source,
            record_id=None,
            kind=kind,
            source_version_id=self.version,
            source_sha256=self.fingerprints[path],
            source_file=path,
            source_member=None,
            source_record=str(ordinal),
            _logical_name=self.logical[path],
            original_values=values,
            original_geometry=bytes(geometry.wkb) if geometry is not None else None,
            **extra,
        )

    def write(self, layer, key, values, transform, *, size=None, total_rows=None):
        signature = content_hash([self.version, self.digest, layer, key])
        completed = self.completed[(layer, key)]
        if any(value != signature for value in completed.values()):
            raise ValueError("Work input signature changed; use a new immutable release")
        finished = self.completed_files.get((layer, key))
        if finished is not None:
            if finished != signature:
                raise ValueError("Work input signature changed; use a new immutable release")
            return
        position = 0
        while f"{key}:{position}" in completed:
            count = self.batch_rows[(layer, f"{key}:{position}")]
            if not count or count < 1:
                raise ValueError("Invalid committed source position")
            position += count
        if total_rows is not None and position > total_rows:
            raise ValueError("Checkpoint exceeds source row count")
        progress_report(self.url, self.source, layer, key, expected_rows=total_rows)
        if callable(values):
            values = values(position)
        else:
            values = islice(values, position, None)
        limit = min(size or self.max_rows, self.max_rows)

        def commit(batch, start):
            self.check_capacity()
            partition = f"{key}:{start}"
            worker = _worker.get()
            try:
                if worker:
                    worker["batch_bytes"] = 0
                    worker["max_batch_bytes"] = self.max_bytes
                with work(self.url, self.source, "import:" + layer, partition, signature) as state:
                    if state is not None:
                        tables = transform(state["connection"], state["run_id"], batch)
                        for table, records in tables.items():
                            state["rows"] += bulk_insert(state["connection"], table, records)
                        state["checkpoint"] = {"record_start": start, "next_record": start + len(batch),
                                               "source_rows": len(batch),
                                               "serialized_bytes": worker.get("batch_bytes", 0) if worker else 0}
                completed[partition] = signature
                self.batch_rows[(layer, partition)] = len(batch)
                return start + len(batch)
            except BatchTooLarge as error:
                if len(batch) == 1:
                    raise BatchTooLarge(f"Oversized unsplittable record: {layer}/{key} position {start}") from error
                middle = len(batch) // 2
                return commit(batch[middle:], commit(batch[:middle], start))
            finally:
                if worker:
                    worker.pop("batch_bytes", None)
                    worker.pop("max_batch_bytes", None)
        try:
            for batch in batches(values, limit, self.max_bytes):
                position = commit(batch, position)
            if total_rows is not None and position != total_rows:
                raise ValueError("Source row count differs from declared expectation")
            with work(self.url, self.source, "import:" + layer, key + ":EOF", signature) as state:
                if state is not None:
                    state["checkpoint"] = {"file_complete": True, "total_source_rows": position,
                                           "next_record": position}
            self.completed_files[(layer, key)] = signature
            progress_report(self.url, self.source, layer, key, state="complete", expected_rows=position)
        except BaseException as error:
            progress_report(self.url, self.source, layer, key, state="failed", error=str(error)[:4000])
            raise

    def check_capacity(self):
        """Optional runtime guard on the filesystem holding the local Docker image."""
        path = os.environ.get("UBEM_CAPACITY_GUARD_PATH")
        if path:
            reserve = int(os.environ.get("UBEM_MIN_FREE_BYTES", str(15 * 1024**3)))
            if shutil.disk_usage(path).free < reserve:
                raise RuntimeError(
                    "National import stopped at the configured free-space reserve; completed work is resumable"
                )

    def finish(self, layer):
        for path, digest in self.fingerprints.items():
            if sha256_file(path) != digest:
                raise ValueError(f"Source changed during national import: {path}")
        with _connect(self.url) as c:
            c.execute("UPDATE catalog.source_file_data SET ready=true WHERE source_version_id=%s AND NOT ready", (self.version,))
            c.execute(
                "UPDATE catalog.dataset_layer SET loading_status='loaded',source_version_id=%s,input_signature=%s WHERE dataset_id=%s AND layer=%s",
                (self.version, content_hash([self.version, self.digest]), self.source, layer),
            )
        self.fingerprints = {}
        self.logical = {}

    def geometry(self):
        root = self.sources.components()
        manifest = json.loads((root / "manifest.json").read_text())
        parts = sorted((root / "components").glob("*.parquet"))
        if set(manifest["partition_sha256"]) != {p.name for p in parts}:
            raise ValueError("Component partitions do not reconcile")
        footprint = self.sources.path("lm_building_geometry_primary")
        self.register(
            "geometry",
            [
                ("building.gpkg", footprint),
                ("manifest.json", root / "manifest.json"),
                ("source-lineage.parquet", root / "source-lineage.parquet"),
                *[(f"components/{p.name}", p) for p in parts],
            ],
            manifest,
        )
        if self.fingerprints[str(footprint.resolve())] != manifest["source_sha256"]:
            raise ValueError("Original geometry differs from component register")
        if (
            self.fingerprints[str((root / "source-lineage.parquet").resolve())]
            != manifest["source_lineage_sha256"]
        ):
            raise ValueError("Component lineage checksum mismatch")
        for path in parts:
            if self.fingerprints[str(path.resolve())] != manifest["partition_sha256"][path.name]:
                raise ValueError("Component partition checksum mismatch")

            def transform(c, run, batch):
                buildings = {}
                components = []
                for r in batch:
                    UUID(r["building_id"])
                    g = shapely.from_wkb(r["geometry"])
                    if normalized_geometry_hash(g) != r["geometry_hash"]:
                        raise ValueError("Component geometry hash mismatch")
                    building = buildings.setdefault(
                        r["building_id"],
                        dict(
                            dataset_id=self.source,
                            building_id=r["building_id"],
                            municipality_values=[],
                        ),
                    )
                    building["municipality_values"] = sorted(
                        set(building["municipality_values"])
                        | set(r.get("municipality_values") or [])
                    )
                    components.append(
                        dict(
                            dataset_id=self.source,
                            component_id=r["component_id"],
                            building_id=r["building_id"],
                            geometry_hash=r["geometry_hash"],
                            geom=g,
                            outline_labels=r.get("outline_labels") or [],
                            representation_status=r.get("representation_status") or "not_assessed",
                            owner_tile_id=r.get("owner_tile_id"),
                            source_fids=r["source_fids"],
                            municipality_values=r.get("municipality_values") or [],
                            revision_timestamp=str(r.get("selected_revision_timestamp")),
                            object_version=r.get("selected_object_version"),
                            run_id=run,
                        )
                    )
                bulk_insert(c, "stock.building", list(buildings.values()))
                return {"stock.geometry_component": components}

            values = (
                r for b in pq.ParquetFile(path).iter_batches(batch_size=8192) for r in b.to_pylist()
            )
            self.write(
                "geometry",
                path.name,
                values,
                transform,
                total_rows=pq.ParquetFile(path).metadata.num_rows,
            )
        lineage = root / "source-lineage.parquet"

        def lineage_transform(c, run, batch):
            return {
                "stock.component_source_feature": [
                    dict(
                        dataset_id=self.source,
                        component_id=r["component_id"],
                        source_fid=r["source_fid"],
                        source_version_id=self.version,
                        disposition=r["disposition"],
                        original_values={k: clean(v) for k, v in r.items()},
                    )
                    for r in batch
                ]
            }

        self.write(
            "geometry",
            "lineage",
            (
                r
                for b in pq.ParquetFile(lineage).iter_batches(batch_size=8192)
                for r in b.to_pylist()
            ),
            lineage_transform,
            total_rows=pq.ParquetFile(lineage).metadata.num_rows,
        )

        def original(c, run, batch):
            return {
                "raw_lm.record": [
                    self.raw(
                        "geometry",
                        footprint,
                        r["_fid"],
                        {},
                    )
                    for r in batch
                ]
            }

        self.write(
            "geometry",
            "original",
            lambda skip: gpkg_fids(footprint, "byggnad", skip),
            original,
        )
        # Aggregate current assertions and routing once, after component loading.
        with _connect(self.url) as c:
            c.execute(
                """WITH owners AS (
                 SELECT building_id,COALESCE(min(owner_tile_id),'unrouted') tile
                 FROM stock.component_data WHERE dataset_id=%s GROUP BY building_id),
                 codes AS (SELECT building_id,array_agg(DISTINCT code ORDER BY code) codes
                 FROM stock.component_data CROSS JOIN LATERAL unnest(municipality_values) code
                 WHERE dataset_id=%s GROUP BY building_id),
                 numbered AS (SELECT o.building_id,o.tile,COALESCE(c.codes,'{}'::text[]) codes,
                 ((row_number() OVER(PARTITION BY o.tile ORDER BY o.building_id)-1)/2000)::text chunk
                 FROM owners o LEFT JOIN codes c USING(building_id)), final AS (
                 SELECT building_id,codes,CASE WHEN cardinality(codes)=1 THEN codes[1] END municipality,
                 tile||':'||chunk partition FROM numbered)
                 UPDATE stock.building b SET municipality_values=n.codes,municipality_code=n.municipality,
                 processing_partition=n.partition FROM final n WHERE b.dataset_id=%s AND b.building_id=n.building_id
                 AND ROW(b.municipality_values,b.municipality_code,b.processing_partition)
                 IS DISTINCT FROM ROW(n.codes,n.municipality,n.partition)""",
                (self.source,) * 3,
            )
            counts = c.execute(
                "SELECT (SELECT count(*) FROM stock.building WHERE dataset_id=%s),(SELECT count(*) FROM stock.geometry_component WHERE dataset_id=%s),(SELECT count(*) FROM stock.component_source_feature WHERE dataset_id=%s)",
                (self.source,) * 3,
            ).fetchone()
            if (counts[1] != manifest["component_count"] or counts[0] != manifest.get(
                "unique_building_uuids", counts[0]
            ) or counts[2] != manifest.get("source_row_count", counts[2])):
                raise ValueError("National geometry counts do not reconcile")
            c.execute("ANALYZE stock.building")
            c.execute("ANALYZE stock.geometry_component")
        self.finish("geometry")

    def register_xml(self):
        root = self.sources.path("lm_building_xml_root")
        locator = self.sources.path("building_registry_locator")
        files = sorted(root.glob("*.xml"))
        self.register("register", [("locator.sqlite", locator), *[(p.name, p) for p in files]])
        # This disposable staging cache is rebuilt on import resume, never per municipality.
        with _connect(self.url) as c:
            c.execute("DROP TABLE IF EXISTS raw_lm.register_locator_staging")
            c.execute(
                "CREATE UNLOGGED TABLE raw_lm.register_locator_staging(building_id uuid PRIMARY KEY,source_file text,source_record text)"
            )
            with sqlite3.connect(f"file:{locator}?mode=ro", uri=True) as db:
                with c.cursor().copy("COPY raw_lm.register_locator_staging FROM STDIN") as copy:
                    for bid, file, ordinal in db.execute(
                        "SELECT building_id,source_file,source_record FROM registry_buildings"
                    ):
                        copy.write_row((bid, file, str(ordinal)))
            c.execute("ANALYZE raw_lm.register_locator_staging")
        for path in files:
            values = (
                parse_building_element(e, source_file=path.name, source_record=n)
                for n, e in _selected_elements(path, "Byggnad", None)
            )

            def transform(c, run, batch):
                raw = [
                    self.raw("register", path, r["source_record"], r["original_values"])
                    for r in batch
                ]
                ids = [r["building_id"] for r in batch if r["building_id"]]
                selected = {
                    str(b): n
                    for b, n in c.execute(
                        "SELECT l.building_id,l.source_record FROM raw_lm.register_locator_staging l JOIN stock.building b ON b.dataset_id=%s AND b.building_id=l.building_id WHERE l.source_file=%s AND l.building_id=ANY(%s::uuid[])",
                        (self.source, path.name, ids),
                    )
                }
                purposes = []
                entrances = []
                facts = []
                for r in batch:
                    if selected.get(r["building_id"]) != str(r["source_record"]):
                        continue
                    status = r["object_status"]
                    existing = (
                        True
                        if status == "Gällande"
                        else False
                        if status in ("Riven", "Avregistrerad")
                        else None
                    )
                    facts.append(
                        dict(
                            dataset_id=self.source,
                            building_id=r["building_id"],
                            object_status=status,
                            status_verified=existing is not None,
                            is_existing=existing,
                            house_number=r["house_number"],
                            construction_year=r["construction_year"],
                            main_building=r["main_building"],
                            register_run_id=run,
                        )
                    )
                    for n, p in enumerate(r["purposes"], 1):
                        purposes.append(
                            dict(
                                dataset_id=self.source,
                                building_id=r["building_id"],
                                ordinal=n,
                                category=p["category"],
                                detail=p["detail"],
                                is_main=p["is_main"],
                                original_values=p["original_values"],
                                run_id=run,
                            )
                        )
                    for n, e in enumerate(r["entrances"]):
                        entrances.append(
                            dict(
                                dataset_id=self.source,
                                building_id=r["building_id"],
                                entrance_id=e["entrance_id"] or f"{r['building_id']}:entrance:{n}",
                                address_place_id=e["address_place_id"],
                                original_values=e["original_values"],
                                run_id=run,
                            )
                        )
                bulk_insert(c, "stock.building", facts, conflict=("dataset_id", "building_id"))
                return {
                    "raw_lm.record": raw,
                    "stock.building_purpose": purposes,
                    "stock.entrance": entrances,
                }

            self.write("register", path.name, values, transform)
        with _connect(self.url) as c:
            if c.execute(
                "SELECT count(*) FROM stock.building b JOIN raw_lm.register_locator_staging l ON b.building_id=l.building_id WHERE b.dataset_id=%s AND b.register_run_id IS NULL",
                (self.source,),
            ).fetchone()[0]:
                raise ValueError("Pinned register locator did not resolve")
            c.execute("DROP TABLE raw_lm.register_locator_staging")
        self.finish("register")

    def addresses(self):
        root = self.sources.path("lm_address_xml_root")
        files = sorted(root.glob("*.xml"))
        self.register("addresses", [(p.name, p) for p in files])
        for path in files:
            values = (
                parse_address_element(e, source_file=path.name, source_record=n)
                for n, e in _selected_elements(path, "Belagenhetsadress", None)
            )

            def transform(c, run, batch):
                addresses = []
                for r in batch:
                    if not r["address_place_id"]:
                        continue
                    k = lm_address(r)
                    # Municipality is supplied by the owning building at lookup time.
                    addresses.append(
                        dict(
                            dataset_id=self.source,
                            address_place_id=r["address_place_id"],
                            street_name=r["street_name"],
                            address_number=r["address_number"],
                            locality=r["locality"],
                            match_key=json.dumps(k[1:], ensure_ascii=False) if k else None,
                            base_match_key=json.dumps(k[1:3] + k[4:], ensure_ascii=False)
                            if k
                            else None,
                            geom=shapely.Point(r["easting"], r["northing"])
                            if r["easting"] is not None
                            else None,
                            original_values={k: v for k, v in r["original_values"].items() if k in (
                                "Belagenhetsadress/Adressomrade/faststalltNamn",
                                "Belagenhetsadress/Adressplatsattribut/Adressplatsbeteckning/adressplatsnummer",
                                "Belagenhetsadress/Adressplatsattribut/Adressplatsbeteckning/bokstavstillagg",
                                "Belagenhetsadress/Adressplatsattribut/postnummer")},
                            run_id=run,
                        )
                    )
                return {
                    "raw_lm.record": [
                        self.raw("address", path, r["source_record"], r["original_values"])
                        for r in batch
                    ],
                    "stock.address_place": addresses,
                }

            self.write("addresses", path.name, values, transform)
        self.finish("addresses")

    def properties(self):
        path = self.sources.path("lm_property_geometry")
        self.register("properties", [("property.gpkg", path)])

        def transform(c, run, batch):
            units = {}
            parts = {}
            labels = {}
            raw = []
            for r in batch:
                p = {k: clean(v) for k, v in r.items() if k not in ("geometry", "_fid", "_source_position")}
                g = r["geometry"]
                raw.append(self.raw("property", path, r["_fid"], p, g))
                unit = p.get("registerenhetsreferens")
                part = p.get("objektidentitet")
                if not unit or not part or g is None or g.is_empty:
                    continue
                units[unit] = dict(dataset_id=self.source, property_unit_id=unit)
                parts[part] = dict(
                    dataset_id=self.source,
                    property_area_part_id=part,
                    property_unit_id=unit,
                    geom=g,
                    run_id=run,
                )
                label = " ".join(v for v in (p.get("trakt"), p.get("etikett")) if v)
                norm = normalize_designation(label)
                if norm:
                    key=(unit,p.get("kommunkod"),norm)
                    position=r["_source_position"]
                    previous=labels.get(key)
                    if previous and (previous["representative_position"]//1024, -previous["representative_position"]) <= (position//1024,-position):
                        continue
                    labels[key] = dict(
                        dataset_id=self.source,
                        property_unit_id=unit,
                        municipality_code=p.get("kommunkod"),
                        raw_label=label,
                        normalized_label=norm,
                        representative_position=position,
                    )
            # This display representative follows the existing source projection's
            # fixed record groups, independent of configurable transport batches.
            bulk_insert(c,"stock.property_unit",list(units.values()))
            bulk_insert(c,"stock.property_designation",list(labels.values()),
                        conflict=("dataset_id","property_unit_id","municipality_code","normalized_label"))
            return {
                "stock.property_area_part": list(parts.values()),
                "raw_lm.record": raw,
            }

        self.write(
            "properties", "features", arrow_rows(path, "registerenhetsomradesyta"), transform
        )
        self.finish("properties")

    def epc_rows(self):
        root = self.sources.path("epc_raw_root")
        files = sorted(root.glob("*/data/*.txt"))
        documents = sorted(root.glob("*/*.xlsx")) + sorted(root.glob("*/*.docx"))
        documents += sorted(root.glob("*/*.pdf"))
        self.register("epc", [(str(p.relative_to(root)), p) for p in files + documents])

        def transform(c, run, batch):
            raw = defaultdict(list)
            certs = {}
            subjects = {}
            for r in batch:
                p = r["original_values"]
                s = epc.subject(r)
                try:
                    fid = epc.strict_value(p["FormularId"], "integer")
                    cert = f"{r['regime']}:{fid}"
                except (KeyError, ValueError, TypeError):
                    fid = cert = None
                table = (
                    "raw_epc.before_v260_row"
                    if r["regime"] == "fore_v260"
                    else "raw_epc.after_v260_row"
                )
                raw[table].append(
                    self.raw(
                        "epc",
                        r["source_file"],
                        r["source_record"],
                        p,
                        certificate_id=cert,
                        subject_id=s["epc_subject_id"] if s else None,
                    )
                )
                if cert:
                    certs[cert] = dict(
                        dataset_id=self.source,
                        certificate_id=cert,
                        formular_id=fid,
                        regime=r["regime"],
                        run_id=run,
                    )
                if s:
                    subjects.setdefault(
                        s["epc_subject_id"],
                        dict(
                            dataset_id=self.source,
                            subject_id=s["epc_subject_id"],
                            certificate_id=cert,
                            municipality_code=s["municipality_code"],
                            designation_normalized=s["designation_normalized"],
                            house_number=s["house_number"],
                            original_values=p,
                            run_id=run,
                        ),
                    )
            return {
                **raw,
                "evidence.epc_certificate": list(certs.values()),
                "evidence.epc_subject": list(subjects.values()),
            }

        self.write("epc", "raw", epc.iter_rows(root), transform)
        with _connect(self.url) as c:
            mappings = [
                dict(
                    zip(
                        [
                            "regime",
                            "source_field",
                            "owner",
                            "value_type",
                            "source_unit",
                            "normalized_unit",
                            "multiplier",
                            "review_status",
                        ],
                        r,
                    )
                )
                for r in c.execute(
                    "SELECT regime,source_field,owner,value_type,source_unit,normalized_unit,multiplier,review_status FROM catalog.field_mapping"
                )
            ]
            cur = c.cursor(name="certificate_projection")
            cur.execute(
                "SELECT certificate_id FROM evidence.epc_certificate WHERE dataset_id=%s ORDER BY certificate_id",
                (self.source,),
            )
            for batch_number, certificates in enumerate(batches((r[0] for r in cur), 256)):
                with work(
                    self.url,
                    self.source,
                    "import:epc-observations",
                    str(batch_number),
                    content_hash([self.version, self.digest, certificates]),
                ) as state:
                    if state is None:
                        continue
                    db = state["connection"]
                    with db.cursor(name="observation_rows") as raw:
                        raw.execute(
                            "SELECT source_file,source_record,original_values,'fore_v260' FROM raw_epc.before_v260_row WHERE dataset_id=%s AND certificate_id=ANY(%s) UNION ALL SELECT source_file,source_record,original_values,'efter_v260' FROM raw_epc.after_v260_row WHERE dataset_id=%s AND certificate_id=ANY(%s)",
                            (self.source, certificates, self.source, certificates),
                        )
                        source_rows = (
                            dict(source_file=f, source_record=n, regime=regime, original_values=p)
                            for f, n, p, regime in raw
                        )
                        observations = epc.observations(
                            source_rows, mappings, collect_locators=False
                        )
                    for chunk in batches(observations, 2048):
                        state["rows"] += bulk_insert(
                            db,
                            "evidence.epc_observation",
                            [
                                dict(dataset_id=self.source, **o, run_id=state["run_id"])
                                for o in chunk
                            ],
                        )
                    dates = [
                        {
                            "certificate_id": o["certificate_id"],
                            "approval": o["normalized_value"]["value"],
                        }
                        for o in observations
                        if o["source_field"] == "ExpertGodkannande"
                        and o["value_status"] == "available"
                    ]
                    db.execute(
                        "UPDATE evidence.epc_certificate e SET certificate_date=d.approval::date,date_status='verified' FROM jsonb_to_recordset(%s) d(certificate_id text,approval text) WHERE e.dataset_id=%s AND e.certificate_id=d.certificate_id",
                        (Jsonb(dates), self.source),
                    )
        self.finish("epc")

    def heights(self):
        from .compact import component_keys

        path = self.sources.path("height_staging_catalogue")
        reconcile = self.sources.path("height_staging_reconciliation")
        mapping_path = self.sources.path("height_path_mapping")
        report = json.loads(reconcile.read_text())
        expected = self.sources.inventory._by_key["height_staging_catalogue"].expected_sha256
        files = [("catalogue.parquet", path), ("reconciliation.json", reconcile)]
        if mapping_path.is_file():
            files.append(("path-mapping.json", mapping_path))
        self.register("heights", files, report)
        if expected and self.fingerprints[str(path.resolve())] != expected:
            raise ValueError("Height catalogue checksum mismatch")
        references = {}
        root = getattr(self.sources, "root", None)
        assets_manifest = Path(root) / "manifests/height-assets.json" if root else None
        if assets_manifest and assets_manifest.is_file():
            declared = json.loads(assets_manifest.read_text())
            for entry in declared["assets"]:
                references[(entry["evidence_root"], entry["original_path"])] = (
                    Path(root) / entry["bundle_path"], entry["sha256"])
            if len(references) != declared["measurement_units"]:
                raise ValueError("Height asset mapping closure differs")
        elif getattr(self.sources, "profile", None) == "synthetic":
            mapping = json.loads(mapping_path.read_text()) if mapping_path.is_file() else {}
            for batch in pq.ParquetFile(path).iter_batches(batch_size=8192, columns=[
                "evidence_record_path", "evidence_record_sha256", "evidence_root"]):
                for r in batch.to_pylist():
                    if r["evidence_record_path"]:
                        p = Path(r["evidence_record_path"])
                        if not p.is_absolute():
                            p = Path(mapping.get("evidence_roots", {}).get(r["evidence_root"], path.parent)) / p
                        references[(r["evidence_root"], r["evidence_record_path"])] = (p, r["evidence_record_sha256"])
        else:
            raise ValueError("Verified bundle-relative height asset mappings are required")
        progress_report(self.url, self.source, "heights", "register measurement assets")
        hashes = {}
        for group in batches(sorted(references.items(), key=lambda item: str(item[0])), 512):
            with _connect(self.url) as c:
                for (evidence_root, original), (p, declared_hash) in group:
                    if not p.is_file():
                        raise FileNotFoundError(f"Declared height measurement missing: {p}")
                    stat = p.stat()
                    cache = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    if cache not in hashes:
                        hashes[cache] = sha256_file(p)
                    digest = hashes[cache]
                    if declared_hash and digest != declared_hash:
                        raise ValueError(f"Height measurement checksum differs: {p}")
                    logical = "measurement/" + content_hash([original, evidence_root])
                    occurrence = self.asset(c, self.version, logical, p, digest)
                    c.execute("INSERT INTO evidence.measurement_source(source_version_id,occurrence_key,evidence_root,original_path) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                              (self.version, occurrence, evidence_root, original))
        progress_report(self.url, self.source, "heights", "register measurement assets", state="complete")

        def transform(c, run, batch):
            raw, heights = [], []
            component_keys(c, self.source, [r["component_id"] for r in batch])
            for r in batch:
                payload = {k: clean(v) for k, v in r.items() if k != "_source_position"}
                raw.append(self.raw("component_height", path, r["_source_position"], payload))
                heights.append(dict(dataset_id=self.source, component_id=r["component_id"],
                                    input_geometry_hash=r["input_geometry_hash"], height_status=r["height_status"] or "missing",
                                    representative_height_m=r.get("representative_height_m"), eave_height_m=r.get("eave_height_m"),
                                    ridge_height_m=r.get("ridge_height_m"), roof_form=r.get("roof_form"),
                                    details={"evidence_resolution": "pending" if r.get("evidence_record_path") else "not_supplied"},
                                    evidence_sha256=None, run_id=run))
            return {"raw_height.record": raw, "evidence.component_height": heights}

        self.write("heights", "catalogue", lambda skip: parquet_rows(path, skip), transform,
                   total_rows=pq.ParquetFile(path).metadata.num_rows)

        def measurement_transform(c, run, batch, asset, requested=None, collected=None):
            asset_key, digest, p, occurrence, logical = asset
            start = min(r["_source_position"] for r in batch)
            end = max(r["_source_position"] for r in batch)
            if requested is None:
                requested = c.execute(
                "SELECT h.component_key,g.building_id::text,encode(g.geometry_hash,'hex'),h.baseline_row_offset,m.evidence_root "
                "FROM evidence.height_catalogue h JOIN stock.component_data g USING(component_key) "
                "JOIN evidence.measurement_source m USING(measurement_key) JOIN catalog.source_file_data f ON f.occurrence_key=m.occurrence_key "
                "WHERE f.asset_key=%s AND (h.baseline_row_offset BETWEEN %s AND %s OR h.baseline_row_offset IS NULL)",
                (asset_key, start, end)).fetchall()
            by_offset = defaultdict(list)
            by_component = defaultdict(list)
            for component, bid, geometry_hash, offset, evidence_root in requested:
                ref = (component, bid, geometry_hash)
                if offset is not None:
                    by_offset[offset].append(ref)
                else:
                    by_component[bid + ":" + geometry_hash].append(ref)
            raw, assignments = [], []
            for value in batch:
                position = value["_source_position"]
                candidates = by_offset.get(position, []) or by_component.get(value.get("component_id"), [])
                if p.suffix != ".parquet" and len(requested) == 1:
                    candidates = [(requested[0][0], requested[0][1], requested[0][2])]
                if not candidates:
                    continue
                if p.suffix == ".parquet":
                    geometry = shapely.from_wkb(value["geometry"]) if value.get("geometry") else None
                    geometry_hash = normalized_geometry_hash(geometry) if geometry is not None else None
                    if any(value.get("building_id") != bid or geometry_hash != expected_hash for _, bid, expected_hash in candidates):
                        raise ValueError(f"Height measurement locator mismatch: {p} row {position}")
                raw.append(self.raw("measurement", p, position, {}))
                assignments.extend((component, position) for component, _, _ in candidates)
            if collected is not None:
                collected[0].extend(raw)
                collected[1].extend((asset_key,component,str(p.resolve()),str(position)) for component,position in assignments)
            elif raw:
                update_measurements(c,raw,[(asset_key,component,str(p.resolve()),str(position)) for component,position in assignments])
            return len(assignments)

        def update_measurements(c,raw,assignments):
            from .compact import locate
            keys=locate(c,raw)
            updates=[dict(asset_key=asset_key,component_key=component,
                          record_key=keys[(self.version,p,"measurement",position)])
                     for asset_key,component,p,position in assignments]
            c.execute("UPDATE evidence.component_height_data h SET evidence_asset_key=v.asset_key,measurement_record_key=v.record_key,details=jsonb_set(h.details,'{evidence_resolution}','\"resolved\"') FROM jsonb_to_recordset(%s) v(asset_key bigint,component_key bigint,record_key bigint) WHERE h.component_key=v.component_key AND (h.evidence_asset_key IS DISTINCT FROM v.asset_key OR h.measurement_record_key IS DISTINCT FROM v.record_key)",
                      (Jsonb(updates),))

        def process_file(asset, count, format):
            asset_key, digest, p, occurrence, logical = asset
            self.fingerprints[str(p.resolve())] = digest
            self.logical[str(p.resolve())] = logical
            if format == "parquet":
                columns=[name for name in ("geometry","building_id","component_id") if name in pq.read_schema(p).names]
                values = lambda skip: parquet_rows(p, skip,columns)
            else:
                values = lambda skip: iter([] if skip else [dict(json.loads(p.read_text()), _source_position=0)])
            def apply(c, run, batch):
                measurement_transform(c, run, batch, asset)
                return {}
            self.write("height-measurement", logical, values, apply, total_rows=count)
            if sha256_file(p) != digest:
                raise ValueError(f"Height measurement changed during import: {p}")
            with _connect(self.url) as c:
                verify_assignments(c, asset_key)
                c.execute("INSERT INTO catalog.asset_verification(asset_key,source_rows) VALUES(%s,%s) ON CONFLICT DO NOTHING", (asset_key, count))
            self.fingerprints.pop(str(p.resolve()))
            self.logical.pop(str(p.resolve()))

        def verify_assignments(c, asset_key):
            unresolved = c.execute("SELECT count(*) FROM evidence.height_catalogue hc JOIN evidence.measurement_source m USING(measurement_key) JOIN catalog.source_file_data f ON f.occurrence_key=m.occurrence_key JOIN evidence.component_height_data h USING(component_key) WHERE f.asset_key=%s AND h.evidence_asset_key IS DISTINCT FROM %s", (asset_key, asset_key)).fetchone()[0]
            if unresolved:
                raise ValueError(f"Height measurement records did not resolve uniquely: asset {asset_key}")

        def process_group(group):
            logicals = [(asset[4], asset[1]) for asset, _, _, _ in group]
            key = "asset-group:" + content_hash(logicals)
            signature = content_hash([self.version, self.digest, logicals])
            worker = _worker.get()
            try:
                with work(self.url, self.source, "import:height-measurement", key, signature) as state:
                    if state is None:
                        return
                    if worker:
                        worker["batch_bytes"] = 0
                        worker["max_batch_bytes"] = self.max_bytes
                    c=state["connection"]
                    requested=defaultdict(list)
                    asset_keys=[asset[0] for asset,_,_,_ in group]
                    for asset_key,*ref in c.execute(
                        "SELECT f.asset_key,h.component_key,g.building_id::text,encode(g.geometry_hash,'hex'),h.baseline_row_offset,m.evidence_root "
                        "FROM evidence.height_catalogue h JOIN stock.component_data g USING(component_key) "
                        "JOIN evidence.measurement_source m USING(measurement_key) JOIN catalog.source_file_data f ON f.occurrence_key=m.occurrence_key "
                        "WHERE f.asset_key=ANY(%s)",(asset_keys,)):
                        requested[asset_key].append(ref)
                    collected=([],[])
                    for asset, count, format, estimate in group:
                        asset_key, digest, p, occurrence, logical = asset
                        self.fingerprints[str(p.resolve())] = digest
                        self.logical[str(p.resolve())] = logical
                        columns=[name for name in ("geometry","building_id","component_id") if name in pq.read_schema(p).names] if format == "parquet" else None
                        values = list(parquet_rows(p,columns=columns)) if format == "parquet" else [dict(json.loads(p.read_text()), _source_position=0)]
                        state["rows"] += measurement_transform(c,state["run_id"],values,asset,requested[asset_key],collected) if values else 0
                        if sha256_file(p) != digest:
                            raise ValueError(f"Height measurement changed during import: {p}")
                        self.fingerprints.pop(str(p.resolve()))
                        self.logical.pop(str(p.resolve()))
                    if collected[0]:
                        update_measurements(c,*collected)
                    unresolved=c.execute("SELECT count(*) FROM evidence.height_catalogue hc JOIN evidence.measurement_source m USING(measurement_key) JOIN catalog.source_file_data f ON f.occurrence_key=m.occurrence_key JOIN evidence.component_height_data h USING(component_key) WHERE f.asset_key=ANY(%s) AND h.evidence_asset_key IS DISTINCT FROM f.asset_key",(asset_keys,)).fetchone()[0]
                    if unresolved:
                        raise ValueError("Height measurement group did not resolve uniquely")
                    bulk_insert(c,"catalog.asset_verification",[dict(asset_key=asset[0],source_rows=count) for asset,count,_,_ in group])
                    state["checkpoint"] = {"source_rows": sum(count for _, count, _, _ in group),
                                           "files_complete": len(group), "asset_keys": [asset[0] for asset,_,_,_ in group]}
            except BatchTooLarge:
                for asset, count, format, estimate in group:
                    process_file(asset,count,format)
                with _connect(self.url) as c:
                    c.execute("UPDATE catalog.work_unit SET state='stale',error='Superseded by bounded file-position work' WHERE owner_dataset_id=%s AND stage='import:height-measurement' AND partition_key=%s AND state='failed'", (self.source,key))
            finally:
                if worker:
                    worker.pop("batch_bytes",None)
                    worker.pop("max_batch_bytes",None)

        last = 0
        while True:
            with _connect(self.url) as c:
                page = c.execute(
                    "SELECT a.asset_key,a.sha256,min(f.bundle_path),min(f.occurrence_key),min(f.logical_name),min(f.format) "
                    "FROM evidence.measurement_source m JOIN catalog.source_file_data f USING(occurrence_key) JOIN catalog.asset a USING(asset_key) "
                    "LEFT JOIN catalog.asset_verification v USING(asset_key) WHERE m.source_version_id=%s AND a.asset_key>%s AND v.asset_key IS NULL "
                    "GROUP BY a.asset_key,a.sha256 ORDER BY a.asset_key LIMIT 128", (self.version,last)).fetchall()
            if not page:
                break
            # The catalogue page closes before file transactions; committed asset receipts
            # make grouping independent of batch limits on an interrupted/resumed import.
            group, group_rows, group_bytes = [],0,0
            for asset_key, digest, relative, occurrence, logical, format in page:
                self.check_capacity()
                p = (Path(root) / relative) if root else next(p for p,h in references.values() if h==digest)
                asset = (asset_key,digest,p,occurrence,logical)
                if format == "parquet":
                    metadata = pq.ParquetFile(p).metadata
                    count = metadata.num_rows
                    estimate = 2*sum(metadata.row_group(n).total_byte_size for n in range(metadata.num_row_groups))
                else:
                    count,estimate = 1,2*p.stat().st_size
                if group and (group_rows+count>self.max_rows or group_bytes+estimate>self.max_bytes):
                    process_group(group)
                    group,group_rows,group_bytes = [],0,0
                if count>self.max_rows or estimate>self.max_bytes:
                    process_file(asset,count,format)
                else:
                    group.append((asset,count,format,estimate))
                    group_rows += count
                    group_bytes += estimate
                last = asset_key
            if group:
                process_group(group)
        self.finish("heights")

    def population(self):
        from .scb import POPULATION_FIELDS, POPULATION_LAYER

        path = self.sources.path("scb_population_grid_2025")
        self.register("population", [("population.gpkg", path)])

        def transform(c, run, batch):
            raw = []
            areas = []
            observations = []
            for r in batch:
                p = {k: clean(v) for k, v in r.items() if k not in ("geometry", "_fid", "_source_position")}
                code = str(r["rutid_scb"])
                period = str(r["referenstid"])
                raw.append(self.raw("population", path, r["_fid"], p, r["geometry"]))
                areas.append(
                    dict(
                        dataset_id=self.source,
                        area_type="population_grid_1km",
                        area_code=code,
                        reference_period=period,
                        geom=r["geometry"],
                        run_id=run,
                    )
                )
                for field in [*POPULATION_FIELDS, "density"]:
                    value = p.get("beftotalt" if field == "density" else field)
                    try:
                        number = epc.strict_value(value, "number") if value is not None else None
                    except (ValueError, TypeError):
                        number = None
                    if field == "density" and number is not None:
                        number /= (float(r["rutstorl"]) / 1000) ** 2
                    observations.append(
                        dict(
                            dataset_id=self.source,
                            observation_id=content_hash([code, field, period]),
                            area_type="population_grid_1km",
                            area_code=code,
                            indicator_id="scb:population_density_1km"
                            if field == "density"
                            else f"scb:population:{field}",
                            reference_period=period,
                            dimensions={"grid_size_m": p.get("rutstorl")},
                            raw_value=value,
                            normalized_value=number,
                            source_unit="person",
                            normalized_unit="person/km2" if field == "density" else "person",
                            transformation="population / cell_size_km_squared"
                            if field == "density"
                            else "identity",
                            value_status="published_protected",
                            provenance={
                                "file": str(path),
                                "fid": str(r["_fid"]),
                                **({} if field == "density" else {"field": field}),
                            },
                            run_id=run,
                        )
                    )
            return {
                "raw_scb.record": raw,
                "context.area": areas,
                "context.observation": observations,
            }

        self.write("population", "grid", arrow_rows(path, POPULATION_LAYER), transform)
        self.finish("population")

    def snapshots(self):
        from .context import project_scb_snapshot
        from .snapshot_paths import snapshot_paths

        snapshots = snapshot_paths(self.sources, self.bundle)
        explicit = os.environ.get("UBEM_SCB_SNAPSHOT")
        if explicit:
            snapshots = [Path(explicit)]
        for snapshot in snapshots:
            if not snapshot.is_dir():
                raise FileNotFoundError(f"Declared SCB snapshot missing: {snapshot}")
            manifest = json.loads((snapshot / "manifest.json").read_text())
            municipality = str(manifest["municipality"])
            frame = municipality_rows(snapshot, municipality)
            projection = project_scb_snapshot(
                snapshot,
                frame.geometry.iloc[0],
                municipality=municipality,
                include_population=False,
            )
            files = [(p.name, p) for p in snapshot.iterdir() if p.is_file()]
            for layer in ("municipality", "regso", "income"):
                self.register(layer, files, manifest)

                def transform(c, run, batch):
                    areas = []
                    relations = []
                    observations = []
                    raw = []
                    if layer == "municipality":
                        areas = [
                            dict(
                                dataset_id=self.source,
                                area_type="municipality",
                                area_code=municipality,
                                name=f"Municipality {municipality}",
                                reference_period=frame.attrs["reference_period"],
                                geom=frame.geometry.iloc[0],
                                run_id=run,
                            )
                        ]
                    elif layer == "regso":
                        for r in projection["raw_areas"]:
                            areas.append(
                                dict(
                                    dataset_id=self.source,
                                    area_type="regso",
                                    area_code=r["area_code"],
                                    name=r["name"],
                                    reference_period=r["reference_period"],
                                    geom=r["geometry_wkb"],
                                    run_id=run,
                                )
                            )
                            relations.append(
                                dict(
                                    dataset_id=self.source,
                                    child_type="regso",
                                    child_code=r["area_code"],
                                    parent_type="municipality",
                                    parent_code=municipality,
                                    run_id=run,
                                )
                            )
                            raw.append(
                                self.raw(
                                    "regso",
                                    snapshot / r["source_file"],
                                    r["source_fid"],
                                    r["original_values"],
                                    shapely.from_wkb(r["geometry_wkb"]),
                                )
                            )
                    else:
                        for r in projection["observations"]:
                            if r["statistics_role"] != "income_statistics":
                                continue
                            observations.append(
                                dict(
                                    dataset_id=self.source,
                                    observation_id=r["observation_id"],
                                    area_type=r["area_type"],
                                    area_code=r["area_code"],
                                    indicator_id=r["indicator_id"],
                                    reference_period=r["reference_period"],
                                    dimensions=r["dimensions"],
                                    raw_value=r["raw_value"],
                                    normalized_value=r["normalized_value"],
                                    source_unit=r["source_unit"],
                                    normalized_unit=r["normalized_unit"],
                                    transformation=r["normalization_method"],
                                    value_status=r["value_status"],
                                    provenance=r["provenance"],
                                    run_id=run,
                                )
                            )
                        for r in projection["raw_pxweb_values"]:
                            raw.append(
                                self.raw(
                                    "income",
                                    snapshot / manifest["income"]["response_file"],
                                    r["source_row"],
                                    r["original_values"],
                                )
                            )
                    return {
                        "raw_scb.record": raw,
                        "context.area": areas,
                        "context.area_relation": relations,
                        "context.observation": observations,
                    }

                self.write(layer, manifest["snapshot_id"], [None], transform)
                with _connect(self.url) as c:
                    c.execute(
                        "UPDATE catalog.dataset SET metadata=jsonb_set(metadata,'{scb_versions}',COALESCE(metadata->'scb_versions','{}'::jsonb)||%s) WHERE dataset_id=%s",
                        (Jsonb({f"{municipality}:{layer}": self.version}), self.source),
                    )
                self.finish(layer)

    def events(self, paths):
        from .bundle import translated_reference
        from .epc_reporting import read_site_events

        for path in paths:
            path = Path(path)
            if not path.is_absolute():
                path = Path(self.sources.root) / path
            path = path.resolve()
            document = json.loads(path.read_text())
            events = read_site_events(path)
            docs = [
                (e["source_document"], translated_reference(path, e["source_document"], source_root=self.sources.root, expected_sha256=e["source_document_sha256"]))
                for e in document.get("events", [])
                if e.get("source_document")
            ]
            self.register("epc_site_events", [("events.json", path), *docs])

            def transform(c, run, batch):
                return {
                    "evidence.epc_site_event": [
                        dict(
                            dataset_id=self.source,
                            **e,
                            source_sha256=self.fingerprints[str(path)],
                            source_version_id=self.version,
                            run_id=run,
                        )
                        for e in events
                    ]
                }

            self.write("epc_site_events", self.version, [None], transform)
            # No dataset_layer entry is needed for optional event sources.
            self.finish("epc_site_events")


@logged_operation("database.source.ingest")
def initialize_sources(
    url, sources, *, source="national", bundle=None, layers=None, batch_rows=None, batch_bytes=None
):
    # Serialize init attempts, including disposable register-locator staging.
    with _connect(url) as lock:
        lock.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("source-init:" + source,)
        )
        with serial_worker(url):
            return _initialize_sources(url, sources, source=source, bundle=bundle, layers=layers,
                                       batch_rows=batch_rows,batch_bytes=batch_bytes)


def field_mapping_digest(c):
    return content_hash(
        c.execute(
            "SELECT regime,source_field,owner,value_type,source_unit,normalized_unit,multiplier,review_status,authority FROM catalog.field_mapping ORDER BY regime,source_field"
        ).fetchall()
    )


def source_projection_digest():
    """Pin source interpretations separately from processing/reporting and indexes."""
    import pyarrow as pa

    from . import compact, hashing, lm_xml, source_inventory
    from .components import normalized_geometry, normalized_geometry_hash
    from .context import _observation, _property, project_scb_snapshot
    from .dataset import LocalSources
    from .epc_assessment import address_key, text
    from .epc_reporting import read_site_events
    from .scb import POPULATION_FIELDS, validate_scb_snapshot
    from .snapshot_paths import snapshot_paths

    methods = {
        name: inspect.getsource(value)
        for name, value in vars(Importer).items()
        if callable(value) and name not in ("__init__", "write", "check_capacity")
    }
    methods.update(
        {
            module.__name__: Path(module.__file__).read_text()
            for module in (epc, hashing, lm_xml, source_inventory, compact)
        }
    )
    methods.update(
        {
            f"{function.__module__}.{function.__name__}": inspect.getsource(function)
            for function in (
                normalized_geometry_hash,
                normalized_geometry,
                arrow_rows,
                parquet_rows,
                gpkg_fids,
                lm_address,
                address_key,
                text,
                project_scb_snapshot,
                _observation,
                _property,
                validate_scb_snapshot,
                read_site_events,
                clean,
                municipality_rows,
                normalize_designation,
                LocalSources,
                snapshot_paths,
            )
        }
    )
    environment = {
        "shapely": shapely.__version__,
        "pyogrio": pyogrio.__version__,
        "gdal": pyogrio.__gdal_version__,
        "pyarrow": pa.__version__,
    }
    contracts = {"checkpoint": "verified-file-record-position-v1"}
    return content_hash(
        ["source-projection-v004", methods, POPULATION_FIELDS, environment, contracts]
    )


def _initialize_sources(url, sources, *, source="national", bundle=None, layers=None,batch_rows=None,batch_bytes=None):
    bundle = bundle or {}
    config_signature = content_hash(bundle)
    with _connect(url) as c:
        existing = c.execute(
            "SELECT sealed,metadata FROM catalog.dataset WHERE dataset_id=%s AND dataset_kind='national_source'",
            (source,),
        ).fetchone()
        if existing and existing[1].get("bundle_signature") != config_signature:
            raise ValueError("Source bundle changed; initialize a fresh backing database")
        mapping = field_mapping_digest(c)
        if existing and existing[1].get("field_mapping_digest") != mapping:
            raise ValueError("Source field mappings changed; initialize a fresh backing database")
        if existing and existing[0]:
            return {"source": source, "status": "already_imported"}
        projection = source_projection_digest()
        if existing and existing[1].get("source_projection_digest") != projection:
            raise ValueError("Source projection code changed; initialize a fresh backing database")
        c.execute(
            "INSERT INTO catalog.dataset(dataset_id,metadata) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (
                source,
                Jsonb(
                    {
                        "bundle_signature": config_signature,
                        "projection_digest": projection,
                        "source_projection_digest": projection,
                        "field_mapping_digest": mapping,
                        "bundle": bundle,
                    }
                ),
            ),
        )
        for layer in SOURCE_LAYERS:
            c.execute(
                "INSERT INTO catalog.dataset_layer(dataset_id,layer) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (source, layer),
            )
    importer = Importer(url, sources, source, bundle,batch_rows,batch_bytes)
    requested = layers or SOURCE_LAYERS
    if set(requested) - set(SOURCE_LAYERS):
        raise ValueError("Unknown source layer")
    result = {}
    handlers = {
        "geometry": importer.geometry,
        "register": importer.register_xml,
        "addresses": importer.addresses,
        "properties": importer.properties,
        "heights": importer.heights,
        "epc": importer.epc_rows,
        "population": importer.population,
    }
    for layer in requested:
        if layer not in handlers:
            continue
        with _connect(url) as c:
            state = c.execute(
                "SELECT loading_status FROM catalog.dataset_layer WHERE dataset_id=%s AND layer=%s",
                (source, layer),
            ).fetchone()[0]
        if state == "loaded":
            result[layer] = "already_imported"
            continue
        try:
            handlers[layer]()
            result[layer] = "loaded"
        except BaseException:
            with _connect(url) as c:
                c.execute(
                    "UPDATE catalog.dataset_layer SET loading_status='failed' WHERE dataset_id=%s AND layer=%s",
                    (source, layer),
                )
            raise
    if any(layer in requested for layer in ("municipality", "regso", "income")):
        importer.snapshots()
    if bundle.get("site_event_files"):
        importer.events(bundle["site_event_files"])
    from .database import install_processing_indexes
    if not layers:
        progress_report(url, source, "finalization", "processing indexes")
        install_processing_indexes(url)
        progress_report(url, source, "finalization", "processing indexes", state="complete")
    with _connect(url) as c:
        for schema in (
            "raw_lm",
            "raw_epc",
            "raw_height",
            "raw_scb",
            "stock",
            "evidence",
            "context",
        ):
            for (table,) in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s", (schema,)
            ).fetchall():
                from psycopg import sql

                c.execute(
                    sql.SQL("ANALYZE {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
                )
        failed = c.execute(
            "SELECT count(*) FROM catalog.dataset_layer WHERE dataset_id=%s AND loading_status='failed'",
            (source,),
        ).fetchone()[0]
        incomplete = c.execute(
            "SELECT layer FROM catalog.dataset_layer WHERE dataset_id=%s AND loading_status<>'loaded'",
            (source,),
        ).fetchall()
        if not layers:
            if failed or incomplete:
                raise ValueError(f"Required source layers are incomplete: {incomplete}")
            c.execute("UPDATE catalog.dataset SET sealed=true WHERE dataset_id=%s", (source,))
    return {"source": source, "status": "imported", "layers": result}
