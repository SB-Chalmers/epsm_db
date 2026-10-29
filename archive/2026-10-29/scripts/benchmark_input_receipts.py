"""Pin sample inputs and measure input rows/bytes outside loader timing."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
import pyarrow.parquet as pq
from sweden_ubem.hashing import sha256_file
from sweden_ubem.asset_operations import publish_json


def prepare(root,verified_journal=None):
    root=Path(root).resolve();receipt=json.loads((root/'receipt.json').read_text())
    groups={'geometry':['buildings.gpkg','register'],'register':['locator.sqlite','xml'],
            'addresses':['addresses'],'properties':['properties.gpkg'],
            'heights':['heights.parquet','reconciliation.json','height-paths.json','measurements'],
            'epc':['epc']}
    checked={};files=[];stage_bytes={}
    if verified_journal:
        proof=Path(__file__).resolve().parents[1]/'output/restart-2026-10-13/source-portability.json'
        report=json.loads(proof.read_text())
        if not report.get('old_root_unavailable') or report.get('verified_occurrences')!=166650:
            raise ValueError('Full bundle verification is required for shared-inode receipt reuse')
        with sqlite3.connect(verified_journal) as c:
            for (encoded,) in c.execute("SELECT identity FROM relocation WHERE state='complete'"):
                original=json.loads(encoded)
                key=(original['device'],original['inode'],original['bytes'],original['mtime_ns'])
                checked[key]=original['sha256']

    for stage,paths in groups.items():
        stage_bytes[stage]=0
        for relative in paths:
            p=root/relative
            children=sorted(p.rglob('*')) if p.is_dir() else [p]
            for path in children:
                if not path.is_file():continue
                stat=path.stat();identity=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
                if identity not in checked:checked[identity]=sha256_file(path)
                files.append(dict(path=str(path.relative_to(root)),stage=stage,bytes=stat.st_size,sha256=checked[identity]))
                stage_bytes[stage]+=stat.st_size
    measurements={item['sha256']:root/item['path'] for item in files
                  if item['stage']=='heights' and item['path'].startswith('measurements/')}
    measured_rows=sum(pq.ParquetFile(p).metadata.num_rows if p.suffix=='.parquet' else 1
                      for p in measurements.values())
    rows=dict(geometry=receipt['components']+receipt['lineage_rows']+receipt['geometry_rows'],
              register=receipt['register_rows'],addresses=receipt['address_rows'],properties=receipt['property_rows'],
              heights=receipt['components']+measured_rows,epc=sum(receipt['epc_rows'].values()))
    result=dict(files=files,stage_input_bytes=stage_bytes,stage_source_rows=rows,
                measured_file_rows=measured_rows,logical_input_bytes=sum(stage_bytes.values()),
                manifest_sha256=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest(),
                verification='Generated files hashed; already verified shared source inodes reused' if verified_journal else 'Every input file hashed')
    publish_json(root/'input-checksums.json',result)
    return {k:v for k,v in result.items() if k!='files'}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('--verified-source-journal',type=Path);a=p.parse_args();print(json.dumps(prepare(a.root,a.verified_source_journal)))
