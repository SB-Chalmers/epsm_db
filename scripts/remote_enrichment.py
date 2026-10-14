"""Immutable follow-on evidence releases, bounded height repair, and promotion gates."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

if __package__:
    from .remote_pipeline import GateFailure,check_capacity,publish,sha256
else:
    from remote_pipeline import GateFailure,check_capacity,publish,sha256


def command(args,**kwargs):
    code=subprocess.run(list(map(str,args)),**kwargs).returncode
    if code:
        raise GateFailure('Enrichment command failed, exit '+str(code))


def clone_sources(original,target):
    """Share untouched data bytes; copy every mutable manifest independently."""
    if target.exists():
        if not (target/'manifests/enrichment-origin.json').exists():
            raise GateFailure('Conflicting or interrupted enrichment destination')
        return
    temporary=target.with_name(target.name+'.staging-'+uuid4().hex)
    for source in original.rglob('*'):
        if source.is_symlink():
            raise GateFailure('Source symlink is not portable')
        if source.is_file():
            relative=source.relative_to(original);destination=temporary/relative
            destination.parent.mkdir(parents=True,exist_ok=True)
            if relative.parts[0]=='manifests':
                shutil.copy2(source,destination)
            else:
                os.link(source,destination)
    publish(temporary/'manifests/enrichment-origin.json',dict(source_root=str(original),
            inventory_sha256=sha256(original/'manifests/file-inventory.json')))
    temporary.rename(target)


def refresh_inventory(root):
    path=root/'manifests/file-inventory.json'
    document=json.loads(path.read_text())
    previous={}
    for row in document['files']:
        previous.setdefault(row['path'],[]).append(row)
    rows=[]
    for asset in sorted(root.rglob('*')):
        if not asset.is_file() or asset.relative_to(root).parts[0]=='manifests':continue
        relative=str(asset.relative_to(root));digest=sha256(asset);size=asset.stat().st_size
        originals=previous.get(relative,[])
        if originals and any(r['sha256']!=digest for r in originals):
            raise GateFailure('Pinned evidence changed without a historical receipt: '+relative)
        rows.extend(originals or [dict(original=str(asset.resolve()),path=relative,sha256=digest,bytes=size,
                                      logical_source='remote-acquired:'+relative)])
        if not any(r['original']==str(asset.resolve()) for r in originals):
            rows.append(dict(original=str(asset.resolve()),path=relative,sha256=digest,bytes=size,
                             logical_source='remote-bundle-alias:'+relative))
    document['files']=rows
    publish(path,document)


def register_snapshot(root,snapshot):
    """Append only the newly acquired assets; final closure verification reads everything."""
    path=root/'manifests/file-inventory.json';document=json.loads(path.read_text())
    existing={(r['path'],r['original']) for r in document['files']}
    assets=[p for p in snapshot.iterdir() if p.is_file()]
    assets.append(root/'scb/population/befolkning_1km_2025.gpkg')
    for asset in assets:
        relative=str(asset.relative_to(root));original=str(asset.resolve())
        if (relative,original) not in existing:
            document['files'].append(dict(original=original,path=relative,sha256=sha256(asset),
                                         bytes=asset.stat().st_size,logical_source='remote-acquired:'+relative))
    publish(path,document)


def acquire_snapshots(config,root,state,original):
    from sweden_ubem.scb import acquire_scb_snapshot,validate_scb_snapshot
    import psycopg
    working=root/'work/enriched/sources'
    clone_sources(original,working)
    names=json.loads((Path(os.environ['UBEM_TRANSPORT_ROOT'])/'application/src/sweden_ubem/municipality_names.json').read_text())['municipalities']
    codes=sorted(names)
    if len(codes)!=290 or any(len(c)!=4 for c in codes):
        raise GateFailure('Pinned municipality roster must contain 290 four-digit codes')
    with psycopg.connect(os.environ['UBEM_DATABASE_URL'],options='-c default_transaction_read_only=on') as c:
        observed={r[0] for r in c.execute("SELECT DISTINCT unnest(municipality_values) FROM stock.building WHERE dataset_id='national'")}
    if observed-set(codes):
        raise GateFailure('Imported municipality assertions exceed the pinned national roster')
    snapshots=state/'scb-snapshots';snapshots.mkdir(exist_ok=True)
    selected={}
    for code in codes:
        check_capacity(root,config['reserve_bytes'])
        receipt=snapshots/(code+'.json')
        if code=='1485':
            from sweden_ubem.dataset import LocalSources
            path=LocalSources('linux-server',original).path('scb_snapshot_uddevalla')
            selected[code]=str(path.relative_to(original))
        elif receipt.exists():
            selected[code]=json.loads(receipt.read_text())['bundle_path']
        else:
            for attempt,delay in enumerate([0,5,30,60]):
                if delay:time.sleep(delay)
                try:
                    result=acquire_scb_snapshot(population_grid=working/'scb/population/befolkning_1km_2025.gpkg',
                            municipality=code,output_root=working/'scb/acquired-national',regso_year=2025,income_year=2024)
                    relative=str(Path(result['path']).relative_to(working))
                    selected[code]=relative
                    register_snapshot(working,Path(result['path']))
                    publish(receipt,dict(municipality=code,bundle_path=relative,snapshot_id=result['snapshot_id']))
                    break
                except Exception:
                    if attempt==3:raise
        report=validate_scb_snapshot(working/selected[code])
        if not report.get('ready'):
            raise GateFailure('SCB snapshot not ready: '+code)
        publish(state/'scb-progress.json',dict(expected_municipalities=290,committed_municipalities=len(selected),municipality=code))
    result=dict(municipalities=selected,regso_year=2025,income_year=2024,expected_municipalities=290)
    publish(state/'scb-coverage.json',result)
    return result


def repair_heights(config,root,state,original,kit):
    tooling=kit/'height-tooling'
    code=json.loads((tooling/'inputs/component-pipeline-code.json').read_text())
    for name,expected in code['sha256'].items():
        if sha256(tooling/name)!=expected:
            raise GateFailure('Height tool fingerprint differs: '+name)
    environment=os.environ.copy()
    environment['PYTHONPATH']=str(tooling/'src')+os.pathsep+str(tooling)
    command([sys.executable,'-m','pip','install','-c',Path(os.environ['UBEM_TRANSPORT_ROOT'])/'requirements-tested.txt','-e',str(tooling),'duckdb>=1,<2','pytest>=8,<10'])
    command([sys.executable,'-m','pytest',tooling/'tests','-q','-m','not integration'],cwd=tooling,env=environment)
    from sweden_ubem.bundle import translated_reference
    oldstage=original/'prepared/heights/component-stage'
    prior=json.loads((oldstage/'manifest.json').read_text())
    paths={}
    for key in ('register','mapping','review','baseline'):
        old=Path(prior[key])/'manifest.json'
        translated=translated_reference(oldstage/'manifest.json',str(old))
        paths[key]=translated.parent
    stage=root/'work/height-repair/component-stage'
    cache=root/'work/height-repair/lidar-cache'
    if not (stage/'manifest.json').exists():
        stage.parent.mkdir(parents=True,exist_ok=True)
        command([sys.executable,tooling/'scripts/run_component_heights.py','prepare','--stage',stage,
                 '--register',paths['register'],'--mapping',paths['mapping'],'--review',paths['review'],
                 '--baseline',paths['baseline'],'--source',original/'lantmateriet/buildings/byggnad_riks.gpkg'],cwd=tooling,env=environment)
        command([sys.executable,'-c','from pathlib import Path; import sys; from scripts.component_overlap_jobs import prepare; prepare(Path(sys.argv[1]))',stage],cwd=tooling,env=environment)
        # Independent copies protect the historical inputs from mutable extraction writes.
        for directory in ('results','receipts','evidence','overlaps'):
            source=oldstage/directory
            if source.exists():
                shutil.copytree(source,stage/directory,dirs_exist_ok=True)
        shutil.copy2(oldstage/'launch-checks.json',stage/'launch-checks.json')
        publish(stage/'remote-origin.json',dict(original_manifest_sha256=sha256(oldstage/'manifest.json'),code_sha256=sha256(tooling/'inputs/component-pipeline-code.json')))
    elif not (stage/'remote-origin.json').exists():
        raise GateFailure('Interrupted height preparation requires inspection; no conflicting files overwritten')
    while True:
        check_capacity(root,config['reserve_bytes'])
        for operation in ('run','overlaps'):
            command([sys.executable,tooling/'scripts/run_component_heights.py',operation,'--stage',stage,
                     '--cache',cache,'--max-owners','10','--max-components-per-owner','200'],cwd=tooling,env=environment)
        command([sys.executable,tooling/'scripts/reconcile_component_heights.py','--stage',stage],cwd=tooling,env=environment)
        report=json.loads((stage/'handoff/reconciliation.json').read_text())
        publish(state/'height-repair-progress.json',report)
        if report['missing_height_records']==0 and report['unresolved_representation_components']==0:
            return dict(stage=str(stage),reconciliation=report)
        # Waiting provider coverage is an external blocker, never a completion receipt.
        import sqlite3
        with sqlite3.connect(stage/'queue.sqlite') as c:
            ready=c.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','processing','retryable_source_error')").fetchone()[0]
        if not ready:
            raise GateFailure('Height coverage/representation remains blocked; see height-repair-progress.json and keyed queues')


def execute_task(task,config):
    root=Path(os.environ['UBEM_REMOTE_ROOT']);state=Path(os.environ['UBEM_REMOTE_STATE'])
    original=Path(os.environ['UBEM_SOURCE_ROOT']);kit=Path(__file__).resolve().parent
    if task=='acquire_scb':return acquire_snapshots(config,root,state,original)
    if task=='repair_heights':return repair_heights(config,root,state,original,kit)
    if task=='enriched_build':return build_enriched(config,root,state,original,kit)
    if task=='final_restore_and_publish':return final_publish(config,root,state,kit)
    raise GateFailure('Unknown enrichment task')


def historical_replace(root,relative,source):
    target=root/relative
    inventory=root/'manifests/file-inventory.json'
    document=json.loads(inventory.read_text())
    history=Path('prepared/heights/history/delivered')/relative
    retained=root/history
    if not retained.exists():
        retained.parent.mkdir(parents=True,exist_ok=True);os.link(target,retained)
        for row in document['files']:
            if row['path']==relative:row['path']=str(history)
    temporary=target.with_name(target.name+'.replacement-'+uuid4().hex)
    shutil.copy2(source,temporary);temporary.replace(target)
    document['files']=[r for r in document['files'] if r['path']!=relative]
    document['files'].append(dict(original=str(source.resolve()),path=relative,sha256=sha256(source),
                                 bytes=source.stat().st_size,logical_source='repaired-height:'+relative))
    publish(inventory,document)


def height_preflight(catalogue,register,output,tooling):
    """Validate every component in Core, keeping failed model cases keyed."""
    command([sys.executable,'-m','pip','install','-e',str(tooling)])
    import duckdb
    import shapely
    from epsm_core import Footprint,ContextBuilding,ContextSpec,BuildingPart,GeometrySpec,ZoningSpec
    con=duckdb.connect();con.execute("SET memory_limit='1GB'");con.execute('SET threads=1')
    con.read_parquet(str(register/'components/*.parquet')).create_view('components')
    con.read_parquet(str(catalogue)).create_view('heights')
    if con.execute('SELECT count(*) FROM components FULL JOIN heights USING(component_id) WHERE components.component_id IS NULL OR heights.component_id IS NULL OR geometry_hash<>input_geometry_hash').fetchone()[0]:
        raise GateFailure('Height/Core component identity or geometry differs')
    reader=con.execute('SELECT component_id,geometry,representative_height_m,height_status,geometry_status,roof_form FROM components JOIN heights USING(component_id) ORDER BY component_id').fetch_record_batch(8192)
    total=failed=0
    with output.open('w') as errors:
        for batch in reader:
            for row in batch.to_pylist():
                total+=1
                try:
                    geometry=shapely.from_wkb(row['geometry'])
                    if geometry.is_empty or not geometry.is_valid:raise ValueError('invalid source geometry')
                    if row['height_status']!='determined':raise ValueError('height not determined')
                    if row['geometry_status'] in ('overlap_measurement_pending','unresolved_roof_evidence'):raise ValueError('representation unresolved')
                    if row['roof_form'] in (None,'unclear'):raise ValueError('roof scenario unresolved')
                    polygons=list(geometry.geoms) if geometry.geom_type=='MultiPolygon' else [geometry]
                    footprints=tuple(Footprint(exterior=tuple(p.exterior.coords)[:-1],
                                     holes=tuple(tuple(h.coords)[:-1] for h in p.interiors)) for p in polygons)
                    # Context uses each physical component's measured height and preserved holes.
                    ContextSpec(buildings=(ContextBuilding(identifier=row['component_id'],footprints=footprints,
                                height_m=row['representative_height_m']),))
                    GeometrySpec(parts=tuple(BuildingPart(identifier=str(index),footprint=footprint,
                                 storey_elevations_m=(0,row['representative_height_m'])) for index,footprint in enumerate(footprints)),
                                 zoning=ZoningSpec(mode='whole_building'),source_crs='EPSG:3006')
                except Exception as error:
                    failed+=1;errors.write(json.dumps(dict(component_id=row['component_id'],reason=str(error)))+'\n')
    con.close()
    result=dict(components=total,failed_components=failed,passed=failed==0,errors=str(output),
                core_geometry_context_contract='component footprints, holes, measured height and identity',
                geometry_check='one measured extrusion per polygon; no inferred source storeys',thermal_simulation_preflight=False)
    if total!=9464839 or failed:
        publish(output.with_suffix('.summary.json'),result)
        raise GateFailure('Core geometry/context preflight has keyed blockers; no publication')
    return result


def build_enriched(config,root,state,original,kit):
    import pyarrow.parquet as pq
    working=root/'work/enriched/sources'
    stage=Path(json.loads((state/'repair_heights-result.json').read_text())['stage'])
    report=json.loads((stage/'handoff/reconciliation.json').read_text())
    if report['missing_height_records'] or report['unresolved_representation_components']:
        raise GateFailure('Height publication coverage is unresolved')
    preflight=height_preflight(stage/'handoff/component-height-catalogue.parquet',original/'prepared/components',
                              state/'core-preflight-errors.jsonl',kit/'epsm-core')
    publish(state/'core-preflight.json',preflight)
    report.update(status='published',blockers=[],epsm_preflight_status='passed',
                  release_status='published',epsm_preflight={'status':'passed','receipt_sha256':sha256(state/'core-preflight.json')},
                  artifact_sha256={'component-height-catalogue.parquet':sha256(stage/'handoff/component-height-catalogue.parquet')})
    publication=state/'height-publication-contract.json';publish(publication,report)
    from sweden_ubem.config import validate_height_release
    published=root/'work/height-repair/publication'
    published.mkdir(exist_ok=True)
    if not (published/'component-height-catalogue.parquet').exists():
        os.link(stage/'handoff/component-height-catalogue.parquet',published/'component-height-catalogue.parquet')
    publish(published/'manifest.json',report)
    validate_height_release(published)
    assets=json.loads((working/'manifests/height-assets.json').read_text())
    lookup={(r['evidence_root'],r['original_path']):r for r in assets['assets']}
    catalogue=stage/'handoff/component-height-catalogue.parquet'
    for batch in pq.ParquetFile(catalogue).iter_batches(batch_size=8192,columns=['evidence_root','evidence_record_path','evidence_record_sha256']):
        for row in batch.to_pylist():
            key=(row['evidence_root'],row['evidence_record_path'])
            if not key[1]:continue
            if key in lookup:
                if lookup[key]['sha256']!=row['evidence_record_sha256']:
                    raise GateFailure('Existing height locator changed its evidence hash')
                continue
            if key[0]!='component_stage':raise GateFailure('Unknown repaired height evidence root')
            source=stage/key[1]
            if not source.resolve().is_relative_to(stage) or sha256(source)!=row['evidence_record_sha256']:
                raise GateFailure('Repaired measurement locator/hash is invalid')
            relative='prepared/heights/repaired-stage/'+key[1]
            target=working/relative;target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists():shutil.copy2(source,target)
            item=dict(evidence_root=key[0],original_path=key[1],bundle_path=relative,sha256=row['evidence_record_sha256'])
            assets['assets'].append(item);lookup[key]=item
    assets['measurement_units']=len(assets['assets']);publish(working/'manifests/height-assets.json',assets)
    for name in ('component-height-catalogue.parquet','component-owner-selection.parquet','reconciliation.json'):
        historical_replace(working,'prepared/heights/component-stage/handoff/'+name,publication if name=='reconciliation.json' else stage/'handoff'/name)
    frozen=working/'prepared/heights/published';frozen.mkdir(exist_ok=True)
    if not (frozen/'component-height-catalogue.parquet').exists():
        os.link(working/'prepared/heights/component-stage/handoff/component-height-catalogue.parquet',frozen/'component-height-catalogue.parquet')
    shutil.copy2(state/'core-preflight.json',frozen/'core-preflight.json')
    publish(frozen/'manifest.json',report)
    refresh_inventory(working)
    coverage=json.loads((state/'scb-coverage.json').read_text())
    application=root/'work/enriched/application'
    if not application.exists():
        shutil.copytree(Path(os.environ['UBEM_TRANSPORT_ROOT'])/'application',application,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
        for name in ('ingest.py','bundle.py','config.py','snapshot_paths.py'):
            shutil.copy2(kit/'enriched-overlay'/name,application/'src/sweden_ubem'/name)
        inventory=application/'config/source_inventory.toml'
        text=inventory.read_text().replace('42a087f8a0fd401f90d5722656dc4612c918d7e7567c0245f77214c87a96df55',sha256(catalogue))
        inventory.write_text(text)
        bundle=application/'config/national-source-bundle.toml'
        entries=[json.dumps(p) for _,p in sorted(coverage['municipalities'].items())]
        bundle.write_text('schema_version = "national-source-bundle-v1"\nsource_id = "national"\nscb_snapshot_paths = ['+', '.join(entries)+']\nsite_event_files = ["site-events/assertions/uddevalla-epc-site-events.json"]\n')
    environment=os.environ.copy();environment['PYTHONPATH']=str(application/'src')
    environment['UBEM_SOURCE_ROOT']=str(working)
    import psycopg
    from psycopg.conninfo import conninfo_to_dict,make_conninfo
    opts=conninfo_to_dict(environment['UBEM_DATABASE_URL']);opts['dbname']='sweden_ubem_v004_enriched'
    enriched=make_conninfo(**opts);environment['UBEM_DATABASE_URL']=enriched
    from psycopg import sql
    with psycopg.connect(make_conninfo(**{**opts,'dbname':'postgres'}),autocommit=True) as c:
        if not c.execute('SELECT 1 FROM pg_database WHERE datname=%s',(opts['dbname'],)).fetchone():
            c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(opts['dbname'])))
    command([sys.executable,kit/'enriched_worker.py','--application',application,'--source-root',working,'--state',state],env=environment,cwd=application)
    return dict(database=opts['dbname'],application=str(application),source_root=str(working),
                measurement_units=assets['measurement_units'],core_preflight=preflight)


def final_publish(config,root,state,kit):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict,make_conninfo
    from compare_public_baseline import public_rows_statement
    enriched=json.loads((state/'enriched_build-result.json').read_text())
    options=conninfo_to_dict(os.environ['UBEM_DATABASE_URL']);options['dbname']=enriched['database']
    original=make_conninfo(**options)
    backup=root/'backups'/('enriched-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'.dump')
    backup.parent.mkdir(exist_ok=True)
    temporary=backup.with_suffix('.dump.incoming')
    with temporary.open('xb') as stream:
        command(['docker','exec','ubem-national-postgis','pg_dump','-U','ubem_owner','-d',options['dbname'],'-Fc'],stdout=stream)
    temporary.replace(backup);backup.chmod(0o600)
    digest=sha256(backup);publish(backup.with_suffix('.checksum.json'),dict(sha256=digest,bytes=backup.stat().st_size))
    name='ubem_test_remote_restore_'+uuid4().hex
    admin=make_conninfo(**{**options,'dbname':'postgres'})
    with psycopg.connect(admin,autocommit=True) as c:c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    with backup.open('rb') as stream:
        command(['docker','exec','-i','ubem-national-postgis','pg_restore','-U','ubem_owner','-d',name,'--exit-on-error','--no-owner','--no-acl'],stdin=stream)
    views=json.loads((Path(os.environ['UBEM_TRANSPORT_ROOT'])/'acceptance/uddevalla/baseline/full-geometry/manifest.json').read_text())['views']
    signatures=[]
    for connection_url in (original,make_conninfo(**{**options,'dbname':name})):
        values={}
        with psycopg.connect(connection_url,options='-c default_transaction_read_only=on') as c:
            for view in views:
                count=total=0
                with c.cursor(name='restore_'+view) as cursor:
                    cursor.execute(public_rows_statement(c,view),('sweden',))
                    for (row,) in cursor:
                        payload=json.dumps(row,sort_keys=True,separators=(',',':'),default=str).encode()
                        total=(total+int.from_bytes(hashlib.sha256(payload).digest(),'big'))%(1<<256);count+=1
                values[view]=dict(rows=count,multiset_sha256_sum=f'{total:064x}')
        signatures.append(values)
    if signatures[0]!=signatures[1]:raise GateFailure('Final restored national public projections differ')
    result=dict(database=enriched['database'],backup=str(backup),sha256=digest,restore_database=name,
                restored_public_views=signatures[0],publication_accepted=True)
    publish(state/'final-restore-verification.json',result)
    with psycopg.connect(admin,autocommit=True) as c:
        if not name.startswith('ubem_test_remote_restore_'):raise GateFailure('Refusing an unowned restore cleanup')
        c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
    publish(root/'active-release.json',dict(database=enriched['database'],source_root=enriched['source_root'],
                                         application=enriched['application'],verification=str(state/'final-restore-verification.json')))
    return result
