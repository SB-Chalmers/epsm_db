"""Evaluate measured loader cost and derive a conservative national capacity budget."""
import argparse
import csv
import json
from pathlib import Path
import shutil
from sweden_ubem.database import application_digest
from sweden_ubem.asset_operations import publish_json
from sweden_ubem.dataset import LocalSources
from sweden_ubem.hashing import sha256_file
from sweden_ubem.ingest import source_projection_digest
from sweden_ubem.epc import detect_encoding

GIB=1024**3


def size(path):
    path=Path(path)
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file()) if path.is_dir() else path.stat().st_size


def epc_input_budget(root):
    """Count files consumed by the loader, preserving archived assets separately."""
    root=Path(root)
    files=sorted(p for p in root.glob('*/data/*.txt')
                 if p.parent.parent.name in ('fore_v260','efter_v260'))
    documents=sorted(root.glob('*/*.xlsx'))+sorted(root.glob('*/*.docx'))+sorted(root.glob('*/*.pdf'))
    records=[]
    for path in files:
        before=path.stat()
        with path.open(encoding=detect_encoding(path),newline='') as stream:
            reader=csv.DictReader(stream,delimiter='|')
            required={'FormularId','IdLankod','IdKommunkod','IdFastBet','IdHusnr'}
            if not required<=set(reader.fieldnames or ()):
                raise ValueError('EPC subject schema mismatch: '+str(path))
            rows=sum(1 for _ in reader)
        digest=sha256_file(path)
        after=path.stat()
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
            raise ValueError('EPC input changed during budget verification: '+str(path))
        records.append(dict(path=str(path.relative_to(root)),bytes=before.st_size,sha256=digest,rows=rows))
    return dict(parsed_files=records,source_rows=sum(r['rows'] for r in records),
                input_bytes=sum(r['bytes'] for r in records)+sum(p.stat().st_size for p in documents),
                registered_document_bytes=sum(p.stat().st_size for p in documents),
                retained_archive_bytes=size(root)-sum(r['bytes'] for r in records)-sum(p.stat().st_size for p in documents))


def verify_view_correction(base,path,environment,old,new):
    """Accept only the independently verified, single view-field type correction."""
    base=Path(base).resolve()
    receipt=json.loads(Path(path).read_text())
    original=base/'new-code';corrected=base/'new-code-lineage-fixed'
    def files(root):
        return {str(p.relative_to(root)):sha256_file(p) for p in root.rglob('*')
                if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
    a=files(original);b=files(corrected)
    baseline='sql/baselines/0.0.4/001_compact.sql'
    changed=[k for k in a if a[k]!=b.get(k)]
    if set(a)!=set(b) or changed!=[baseline]:raise ValueError('Correction changes more than its declared view')
    before="'object_version',l.object_version,'outline'"
    after="'object_version',l.object_version::text,'outline'"
    original_sql=(original/baseline).read_text()
    if original_sql.count(before)!=1 or original_sql.replace(before,after)!=(corrected/baseline).read_text():
        raise ValueError('Correction is not the single declared view-field cast')
    differences=[k for k in old['facts'] if old['facts'][k]!=new['facts'].get(k)]
    checks=[receipt.get('accepted') is True,
            receipt.get('original_code_digest')==environment['application_digest'],
            receipt.get('corrected_code_digest')==application_digest(),
            receipt.get('source_projection_digest')==source_projection_digest(),
            receipt.get('old_benchmark_sha256')==sha256_file(base/'old-1048576-final.json'),
            receipt.get('new_benchmark_sha256')==sha256_file(base/'new-1048576-final.json'),
            receipt.get('source_lineage_sha256')==sha256_file(base/'samples/1048576/register/source-lineage.parquet'),
            receipt.get('original_lineage')==new['facts']['lineage'],
            receipt.get('expected_lineage')==old['facts']['lineage'],
            receipt.get('corrected_lineage')==old['facts']['lineage'],
            differences==['lineage'],receipt.get('original_differing_groups')==differences]
    if not all(checks):raise ValueError('View-correction verification receipt does not match this run')
    return dict(path=str(Path(path).resolve()),sha256=sha256_file(path),**receipt)


def accept(base,source_root,output,view_correction=None):
    base=Path(base)
    old=json.loads((base/'old-1048576-final.json').read_text())
    new=json.loads((base/'new-1048576-final.json').read_text())
    sample=base/'samples/1048576'
    inputs=json.loads((sample/'input-checksums.json').read_text())
    prepared=json.loads((sample/'receipt.json').read_text())
    environment=json.loads((base/'environment.json').read_text())
    if not old.get('finished') or not new.get('finished'):raise ValueError('Final paired runs incomplete')
    differences={k:dict(old=old['facts'][k],new=new['facts'][k]) for k in old['facts'] if old['facts'][k]!=new['facts'][k]}
    correction=None
    if set(old['facts'])!=set(new['facts']):raise ValueError('Consumer evidence groups differ')
    if view_correction:
        correction=verify_view_correction(base,view_correction,environment,old,new)
    else:
        if environment['application_digest']!=application_digest():raise ValueError('Code changed after benchmark freeze')
        if differences:raise ValueError('Consumer evidence differs')
    sources=LocalSources(root=source_root)
    epc_budget=epc_input_budget(sources.path('epc_raw_root'))
    full_bytes={
      'geometry':size(sources.path('lm_building_geometry_primary'))+size(sources.components()),
      'register':size(sources.path('building_registry_locator'))+size(sources.path('lm_building_xml_root')),
      'addresses':size(sources.path('lm_address_xml_root')),
      'properties':size(sources.path('lm_property_geometry')),
      'heights':size(sources.path('height_staging_catalogue')),
      'epc':epc_budget['input_bytes']}
    inventory=json.loads((Path(source_root)/'manifests/file-inventory.json').read_text())['files']
    height_assets=json.loads((Path(source_root)/'manifests/height-assets.json').read_text())['assets']
    bypath={r['path']:r for r in inventory}
    physical={r['sha256']:bypath[r['bundle_path']]['bytes'] for r in height_assets}
    full_bytes['heights']+=sum(physical.values())
    rows_factor=9464839/prepared['components']
    ratios={k:full_bytes[k]/inputs['stage_input_bytes'][k] for k in full_bytes}
    factors={k:max(ratios[k],rows_factor if k in ('geometry','register','heights') else 1.0) for k in ratios}
    factors['epc']=max(factors['epc'],epc_budget['source_rows']/inputs['stage_source_rows']['epc'])
    stages={k:dict(seconds_old=old['stages'][k]['seconds'],seconds_new=new['stages'][k]['seconds'],
                  source_rows=inputs['stage_source_rows'][k],input_bytes=inputs['stage_input_bytes'][k],
                  new_rows_per_second=inputs['stage_source_rows'][k]/new['stages'][k]['seconds'],
                  new_bytes_per_second=inputs['stage_input_bytes'][k]/new['stages'][k]['seconds'],
                  national_work_factor=factors[k]) for k in ratios}
    projected_seconds=sum(new['stages'][k]['seconds']*factors[k] for k in ratios)
    max_factor=max(factors.values())
    file_factor=len(height_assets)/prepared['measurement_files']
    estimated=0
    allocation={}
    for table,heap,index in new['storage']['relations']:
        if table.startswith('raw_epc.') or table.startswith('evidence.epc_'):factor=factors['epc']
        elif table in ('stock.address_place','stock.entrance'):factor=max(factors['addresses'],factors['register'])
        elif table.startswith('stock.property_') or table=='raw_lm.property_diagnostic':factor=factors['properties']
        elif table in ('catalog.asset','catalog.asset_location_data','catalog.source_file_data','catalog.asset_verification','evidence.measurement_source'):factor=max(1,file_factor)
        else:factor=max_factor
        allocation[table]=dict(sample_bytes=heap+index,work_factor=factor,projected_bytes=int((heap+index)*factor))
        estimated+=int((heap+index)*factor)
    # Variable geometry/evidence distributions, context and Uddevalla processing
    # warrant explicit headroom. This is a capacity guard, not a footprint promise.
    estimated_final=int(estimated*1.35)+6*GIB
    reserve=15*GIB
    transient=max(8*GIB,int(estimated_final*.15))
    capacity=estimated_final+reserve+transient
    reduced=dict(storage=new['storage']['database_bytes']<old['storage']['database_bytes'],
                 wall_time=new['loader_seconds']<old['loader_seconds'],wal=new['wal_bytes']<old['wal_bytes'])
    result=dict(replacement_code_digest=application_digest(),exact_consumer_facts=True,stages=stages,
                independently_verified_view_correction=correction,
                measured_old=dict(seconds=old['loader_seconds'],database_bytes=old['storage']['database_bytes'],wal_bytes=old['wal_bytes'],peak_worker_rss_bytes=old['peak_worker_rss_bytes'],temporary_bytes=old['temporary_bytes']),
                measured_new=dict(seconds=new['loader_seconds'],database_bytes=new['storage']['database_bytes'],wal_bytes=new['wal_bytes'],peak_worker_rss_bytes=new['peak_worker_rss_bytes'],temporary_bytes=new['temporary_bytes']),
                improvements=reduced,full_source_input_bytes=full_bytes,epc_input_budget=epc_budget,allocation=allocation,
                measured_work_projection_seconds=projected_seconds,
                national_loading_budget_seconds=[int(projected_seconds*1.5),int(projected_seconds*3)],
                estimated_final_database_bytes=estimated_final,free_space_reserve_bytes=reserve,
                transient_space_allowance_bytes=transient,capacity_requirement_bytes=capacity,
                available_filesystem_bytes=shutil.disk_usage(source_root).free,
                cache_and_sampling_limits=environment['cache_policy']+' National budgets allow for distributions, cold I/O, context, finalization and recovery; these are planning bounds, not a completion-time guarantee.',
                recovery=dict(old_65536=json.loads((base/'old-65536-corrected.json').read_text())['interrupted_resume_seconds'],new_65536=json.loads((base/'new-65536-corrected.json').read_text())['interrupted_resume_seconds']),
                uddevalla_acceptance='pending separate gate',national_publication='blocked by existing height coverage/representation and EPSM preflight gates')
    result['national_restart_accepted']=all(reduced.values()) and result['available_filesystem_bytes']>=capacity
    publish_json(output,result)
    if not result['national_restart_accepted']:raise ValueError('Measured cost or capacity gate did not pass')
    return result


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--benchmarks',type=Path,required=True);p.add_argument('--source-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--view-correction',type=Path);a=p.parse_args();print(json.dumps(accept(a.benchmarks,a.source_root,a.output,a.view_correction)))
