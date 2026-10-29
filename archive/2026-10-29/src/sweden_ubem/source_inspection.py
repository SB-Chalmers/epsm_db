"""Explicit inspection of verified originals; normalized projections are never originals."""
import csv
import json
from pathlib import Path
import sqlite3
from xml.parsers import expat

import pyarrow.parquet as pq

from .asset_operations import safe_relative
from .database import _connect
from .epc import detect_encoding
from .hashing import sha256_file
from .source_inventory import DEFAULT_SOURCE_ROOT


def xml_member(path, name, ordinal):
    starts = []
    count = 0
    found = None
    parser = expat.ParserCreate(namespace_separator="|")
    class Found(Exception):
        pass
    def start(tag, attributes):
        if tag.rsplit("|", 1)[-1] == name:
            starts.append(parser.CurrentByteIndex)
    def end(tag):
        nonlocal count, found
        if tag.rsplit("|", 1)[-1] == name:
            first = starts.pop()
            count += 1
            if count == ordinal:
                found = (first, parser.CurrentByteIndex)
                raise Found()
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                parser.Parse(block, False)
            parser.Parse(b"", True)
    except Found:
        pass
    if found is None:
        raise ValueError("Registered XML member ordinal does not exist")
    first, last = found
    with path.open("rb") as stream:
        stream.seek(first)
        opening = stream.read(65536).split(b">", 1)[0]
        if opening.rstrip().endswith(b"/"):
            last = first + len(opening) + 1
        else:
            stream.seek(last)
            ending = stream.read(65536).split(b">", 1)[0]
            last += len(ending) + 1
        stream.seek(first)
        fragment = stream.read(last - first)
    return {"encoding": "original-xml-bytes", "hex": fragment.hex()}


def read_original(path, format, kind, locator, metadata):
    if format == "gpkg":
        from .scb import POPULATION_LAYER
        layer = metadata.get("layer") or {"geometry": "byggnad", "building_feature": "byggnad", "property": "registerenhetsomradesyta", "population": POPULATION_LAYER}.get(kind)
        if not layer:
            raise ValueError("Registered GeoPackage layer is missing")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as c:
            c.row_factory = sqlite3.Row
            columns = c.execute('PRAGMA table_info("' + layer.replace('"', '""') + '")').fetchall()
            primary = [row[1] for row in columns if row[5]]
            if len(primary) != 1:
                raise ValueError("GeoPackage feature primary key is ambiguous")
            statement = 'SELECT * FROM "' + layer.replace('"', '""') + '" WHERE "' + primary[0].replace('"', '""') + '"=?'
            rows = c.execute(statement, (int(locator),)).fetchall()
            if len(rows) != 1:
                raise ValueError("Registered GeoPackage FID does not resolve uniquely")
            return {key: {"encoding": "source-blob", "hex": value.hex()} if isinstance(value, bytes) else value for key, value in dict(rows[0]).items()}
    if format == "xml":
        name = {"register": "Byggnad", "address": "Belagenhetsadress"}.get(kind)
        if not name:
            raise ValueError("Registered XML member type is missing")
        return xml_member(path, name, int(locator))
    if format == "txt":
        with path.open(encoding=detect_encoding(path), newline="") as stream:
            reader = csv.reader(stream, delimiter="|")
            header = next(reader)
            for ordinal, row in enumerate(reader, 1):
                if ordinal == int(locator):
                    return {"header": header, "fields": row}
        raise ValueError("Registered EPC row ordinal does not exist")
    if format == "parquet":
        file = pq.ParquetFile(path)
        if str(locator).isdecimal():
            offset = int(locator)
            start = 0
            for group in range(file.num_row_groups):
                size = file.metadata.row_group(group).num_rows
                if start <= offset < start + size:
                    position = start
                    for batch in file.iter_batches(batch_size=8192, row_groups=[group]):
                        if position <= offset < position + batch.num_rows:
                            return batch.slice(offset - position, 1).to_pylist()[0]
                        position += batch.num_rows
                start += size
        else:
            field = metadata.get("locator_column") or ("area_code" if kind == "regso" else "component_id")
            found = []
            for batch in file.iter_batches(batch_size=8192):
                found.extend(row for row in batch.to_pylist() if str(row.get(field)) == str(locator))
            if len(found) == 1:
                return found[0]
        raise ValueError("Registered Parquet row locator is missing or ambiguous")
    if format == "json":
        document = json.loads(path.read_text())
        if metadata.get("json_pointer"):
            for part in metadata["json_pointer"].strip("/").split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                document = document[int(part)] if isinstance(document, list) else document[part]
        return document
    raise ValueError(f"Original inspection is unsupported for registered format {format!r}")


def inspect_record(url, record_id, source_root=None):
    import os
    root = Path(source_root or os.environ.get("UBEM_SOURCE_ROOT") or DEFAULT_SOURCE_ROOT).expanduser().resolve()
    try:
        numeric_id = int(record_id)
        if not 0 < numeric_id < 2**63:
            raise ValueError()
    except ValueError:
        raise ValueError(f"Unknown registered record ID: {record_id}") from None
    with _connect(url) as c:
        c.execute("SET TRANSACTION READ ONLY")
        row = c.execute(
            "SELECT bundle_path,asset_sha256,format,kind,source_record,metadata,logical_name,source_version_id "
            "FROM mart.source_record_locators WHERE record_id=%s", (numeric_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown registered record ID: {record_id}")
    relative, expected, format, kind, locator, metadata, logical, version = row
    path = root / safe_relative(relative)
    if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Original source asset missing: {path}")
    if sha256_file(path) != expected:
        raise ValueError(f"Original source asset checksum changed: {path}")
    return {"record_id": record_id, "source_version_id": version, "logical_source": logical,
            "bundle_path": relative, "asset_sha256": expected, "format": format,
            "locator": {"kind": kind, "record_position": locator},
            "original_record": read_original(path, format, kind, locator, metadata)}
