from pathlib import Path
import sqlite3
import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from sweden_ubem.source_inspection import read_original, xml_member


def test_xml_original_bytes_keep_namespaces_whitespace_and_entities(tmp_path):
    original = b'<root xmlns:b="urn:test"><b:Byggnad a="1"> x &amp; y </b:Byggnad><b:Byggnad/></root>'
    path = tmp_path / "input.xml"
    path.write_bytes(original)
    assert bytes.fromhex(xml_member(path, "Byggnad", 1)["hex"]) == b'<b:Byggnad a="1"> x &amp; y </b:Byggnad>'
    assert bytes.fromhex(xml_member(path, "Byggnad", 2)["hex"]) == b'<b:Byggnad/>'
    with pytest.raises(ValueError, match="ordinal"):
        xml_member(path, "Byggnad", 3)


def test_feature_inspection_uses_actual_fid_not_row_offset(tmp_path):
    path = tmp_path / "input.gpkg"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE byggnad(fid INTEGER PRIMARY KEY,value TEXT,geom BLOB)")
        c.executemany("INSERT INTO byggnad VALUES(?,?,?)", [(2,"first",b"source geometry"),(9,"second",b"other")])
    assert read_original(path,"gpkg","geometry","9",{})["value"] == "second"
    assert read_original(path,"gpkg","geometry","2",{})["geom"]["hex"] == b"source geometry".hex()
    with pytest.raises(ValueError, match="FID"):
        read_original(path,"gpkg","geometry","1",{})


def test_csv_original_retains_duplicate_columns_and_unparsed_values(tmp_path):
    path = tmp_path / "epc.txt"
    path.write_text("A|A|B\n 01 |two|NaN\n",encoding="latin-1")
    assert read_original(path,"txt","epc","1",{}) == {"header":["A","A","B"],"fields":[" 01 ","two","NaN"]}


def test_parquet_locator_is_zero_based_across_row_groups(tmp_path):
    path = tmp_path / "input.parquet"
    pq.write_table(pa.table({"value":[1,2,3,4]}),path,row_group_size=2)
    assert read_original(path,"parquet","measurement","0",{}) == {"value":1}
    assert read_original(path,"parquet","measurement","2",{}) == {"value":3}
    with pytest.raises(ValueError,match="locator"):
        read_original(path,"parquet","measurement","4",{})
