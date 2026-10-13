"""Prepare immutable real-source loader samples with explicit preparation receipts."""
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import sqlite3
import xml.etree.ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

from sweden_ubem.asset_operations import publish_json
from sweden_ubem.epc import detect_encoding, strict_value
from sweden_ubem.hashing import sha256_file
from sweden_ubem.lm_xml import _selected_elements

ROOT=Path(__file__).resolve().parents[1]
RAW=ROOT/'data/raw/national-2026-10-13'


def quote(name):
    return '"'+name.replace('"','""')+'"'


def clone_gpkg(source,target,layer,*,fids=None,count=None):
    target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dst:
        for pragma in ('application_id','user_version'):
            dst.execute(f'PRAGMA {pragma}='+str(src.execute(f'PRAGMA {pragma}').fetchone()[0]))
        for name,sql in src.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND (name LIKE 'gpkg_%' OR name=?)",(layer,)):
            dst.execute(sql)
            if name!=layer:
                rows=src.execute('SELECT * FROM '+quote(name)).fetchall()
                if rows:
                    dst.executemany('INSERT INTO '+quote(name)+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
        if dst.execute("SELECT 1 FROM sqlite_master WHERE name='gpkg_extensions'").fetchone():
            dst.execute("DELETE FROM gpkg_extensions WHERE extension_name='gpkg_rtree_index'")
        primary=next(row[1] for row in src.execute('PRAGMA table_info('+quote(layer)+')') if row[5])
        if fids is None:
            selected=src.execute('SELECT '+quote(primary)+' FROM '+quote(layer)+' ORDER BY '+quote(primary)+' LIMIT ?',(count,)).fetchall()
            fids=[r[0] for r in selected]
        for start in range(0,len(fids),8192):
            keys=fids[start:start+8192]
            values=src.execute('SELECT * FROM '+quote(layer)+' WHERE '+quote(primary)+' IN ('+','.join('?' for _ in keys)+')',keys).fetchall()
            if values:
                dst.executemany('INSERT INTO '+quote(layer)+' VALUES('+','.join('?' for _ in values[0])+')',values)
        if dst.execute("SELECT 1 FROM sqlite_master WHERE name='gpkg_ogr_contents'").fetchone():
            dst.execute('UPDATE gpkg_ogr_contents SET feature_count=? WHERE table_name=?',(len(fids),layer))
    return len(fids)


def filtered_parquet(source,target,field,wanted):
    writer=None
    total=0
    try:
        for batch in pq.ParquetFile(source).iter_batches(batch_size=8192):
            mask=pa.array([value in wanted for value in batch.column(field).to_pylist()])
            chosen=batch.filter(mask)
            if chosen.num_rows:
                if writer is None:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    writer=pq.ParquetWriter(target,chosen.schema)
                writer.write_batch(chosen)
                total+=chosen.num_rows
    finally:
        if writer:writer.close()
    return total


def prepare(size,output):
    output=Path(output).resolve()
    receipt=output/'receipt.json'
    if receipt.exists():
        return json.loads(receipt.read_text())
    if output.exists():
        raise ValueError('Interrupted sample preparation must be inspected, not overwritten')
    output.mkdir(parents=True)
    register=output/'register'
    (register/'components').mkdir(parents=True)
    writer=None
    ids=set()
    buildings=set()
    last=None
    selected=0
    # Extend the last UUID boundary so a sample never truncates a component cohort.
    stop=False
    for path in sorted((RAW/'prepared/components/components').glob('*.parquet')):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
            values=[]
            for row in batch.to_pylist():
                if selected>=size and row['building_id']!=last:
                    stop=True
                    break
                values.append(row)
                ids.add(row['component_id']);buildings.add(row['building_id'])
                last=row['building_id'];selected+=1
            if values:
                table=pa.Table.from_pylist(values,schema=batch.schema)
                if writer is None:writer=pq.ParquetWriter(register/'components/part-00000.parquet',table.schema)
                writer.write_table(table)
            if stop:break
        if stop:break
    writer.close()
    lineage=register/'source-lineage.parquet'
    lineage_count=filtered_parquet(RAW/'prepared/components/source-lineage.parquet',lineage,'component_id',ids)
    fids=[]
    for batch in pq.ParquetFile(lineage).iter_batches(batch_size=8192,columns=['source_fid']):
        fids.extend(batch.column(0).to_pylist())
    geometry=output/'buildings.gpkg'
    geometry_count=clone_gpkg(RAW/'lantmateriet/buildings/byggnad_riks.gpkg',geometry,'byggnad',fids=sorted(set(fids)))
    publish_json(register/'manifest.json',dict(component_count=selected,unique_building_uuids=len(buildings),
        source_row_count=lineage_count,source_sha256=sha256_file(geometry),source_lineage_sha256=sha256_file(lineage),
        partition_sha256={'part-00000.parquet':sha256_file(register/'components/part-00000.parquet')}))
    print(f'Sample {size}: geometry {selected}, lineage {lineage_count}',flush=True)
    property_file=output/'properties.gpkg'
    property_count=clone_gpkg(RAW/'lantmateriet/properties/fastighet_riks.gpkg',property_file,'registerenhetsomradesyta',count=size)

    # Keep the exact original registry selection, remapping its ordinal only within the sample XML.
    wanted={}
    with sqlite3.connect((RAW/'prepared/register/registry.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as src:
        uuids=sorted(buildings)
        for start in range(0,len(uuids),8192):
            group=uuids[start:start+8192]
            for bid,file,ordinal in src.execute('SELECT building_id,source_file,source_record FROM registry_buildings WHERE building_id IN ('+','.join('?' for _ in group)+')',group):
                wanted.setdefault(file,{})[int(ordinal)]=bid
    xmlroot=output/'xml';xmlroot.mkdir()
    with sqlite3.connect(output/'locator.sqlite') as dst:
        dst.execute('CREATE TABLE registry_buildings(building_id text PRIMARY KEY,source_file text,source_record integer)')
        for file,ordinals in wanted.items():
            selected_ids=set(ordinals.values())
            counter=0
            with (xmlroot/file).open('wb') as stream:
                stream.write(b'<root>')
                for original,element in _selected_elements(RAW/'lantmateriet/buildings/xml'/file,'Byggnad',selected_ids):
                    if original not in ordinals:continue
                    counter+=1
                    stream.write(ET.tostring(element,encoding='utf-8'))
                    dst.execute('INSERT INTO registry_buildings VALUES(?,?,?)',(ordinals[original],file,counter))
                stream.write(b'</root>')
    addresses=output/'addresses';addresses.mkdir()
    address_count=0
    for path in sorted((RAW/'lantmateriet/addresses/xml').glob('*.xml')):
        with (addresses/path.name).open('wb') as stream:
            stream.write(b'<root>')
            for ordinal,element in _selected_elements(path,'Belagenhetsadress',None):
                stream.write(ET.tostring(element,encoding='utf-8'));address_count+=1
                if address_count>=size:break
            stream.write(b'</root>')
        if address_count>=size:break
    print(f'Sample {size}: XML buildings {sum(len(v) for v in wanted.values())}, addresses {address_count}',flush=True)

    catalogue=output/'heights.parquet'
    filtered_parquet(RAW/'prepared/heights/component-stage/handoff/component-height-catalogue.parquet',catalogue,'component_id',ids)
    # Sample paths are translated without changing the untouched measured asset bytes.
    mappings={(r['evidence_root'],r['original_path']):r for r in json.loads((RAW/'manifests/height-assets.json').read_text())['assets']}
    records=[]
    measure={}
    for batch in pq.ParquetFile(catalogue).iter_batches(batch_size=8192):
        for row in batch.to_pylist():
            if row['evidence_record_path']:
                asset=mappings[(row['evidence_root'],row['evidence_record_path'])]
                source=RAW/asset['bundle_path']
                relative='measurements/'+asset['sha256']+'/'+source.name
                target=output/relative
                if not target.exists():
                    target.parent.mkdir(parents=True,exist_ok=True);os.link(source,target)
                row['evidence_root']='sample_measurements'
                row['evidence_record_path']=relative
                measure[relative]=dict(evidence_root='sample_measurements',original_path=relative,bundle_path=relative,sha256=asset['sha256'])
            records.append(row)
    pq.write_table(pa.Table.from_pylist(records),catalogue)
    publish_json(output/'reconciliation.json',dict(status='publication_blocked',component_rows=selected,
        unique_component_ids=selected,unique_building_uuids=len(buildings),sha256={catalogue.name:sha256_file(catalogue)},blockers=['Benchmark sample, not publishable']))
    publish_json(output/'height-paths.json',dict(path_prefixes=[],evidence_roots={'sample_measurements':str(output)}))
    publish_json(output/'manifests/height-assets.json',dict(measurement_units=len(measure),assets=list(measure.values())))

    epcroot=output/'epc';epc_counts={}
    for regime in ('fore_v260','efter_v260'):
        originals=sorted((RAW/'boverket/epc'/regime/'data').glob('*.txt'))
        certificates=set()
        for file in originals:
            with file.open(encoding=detect_encoding(file),newline='') as stream:
                for row in csv.DictReader(stream,delimiter='|'):
                    try:certificates.add(strict_value(row['FormularId'],'integer'))
                    except (ValueError,TypeError,KeyError):continue
                    if len(certificates)>=size:break
            if len(certificates)>=size:break
        count=0
        for file in originals:
            target=epcroot/regime/'data'/file.name;target.parent.mkdir(parents=True,exist_ok=True)
            encoding=detect_encoding(file)
            with file.open(encoding=encoding,newline='') as source, target.open('w',encoding=encoding,newline='') as destination:
                reader=csv.DictReader(source,delimiter='|')
                writer=csv.DictWriter(destination,fieldnames=reader.fieldnames,delimiter='|')
                writer.writeheader()
                for row in reader:
                    try:chosen=strict_value(row['FormularId'],'integer') in certificates
                    except (ValueError,TypeError,KeyError):chosen=False
                    if chosen:writer.writerow(row);count+=1
        for document in (RAW/'boverket/epc'/regime).iterdir():
            if document.is_file():os.link(document,epcroot/regime/document.name)
        epc_counts[regime]=count
    report=dict(requested_components=size,components=selected,buildings=len(buildings),lineage_rows=lineage_count,
        geometry_rows=geometry_count,property_rows=property_count,address_rows=address_count,register_rows=sum(len(v) for v in wanted.values()),
        measurement_files=len(measure),epc_rows=epc_counts,source_bundle=str(RAW),assessment_date='2026-10-12',
        preparation='Original feature FIDs retained; complete UUID cohorts and EPC certificates; sample XML/CSV framed without source-value normalization; original measured files hard-linked read-only')
    publish_json(receipt,report)
    print(json.dumps(report),flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--rows',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    prepare(args.rows,args.output)
