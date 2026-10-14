"""Application tasks executed by the remote receipt-gated supervisor."""
import argparse
from datetime import datetime, timezone
import json
import os
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from remote_pipeline import GateFailure, check_capacity, publish, sha256

def interrupted(signum,frame):
    raise SystemExit(130)

signal.signal(signal.SIGTERM,interrupted)


def execute(command, **kwargs):
    result=subprocess.run(list(map(str,command)),**kwargs)
    if result.returncode:
        raise GateFailure('Task command failed, exit '+str(result.returncode))


def main(task,config):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict,make_conninfo
    from sweden_ubem.database import application_digest,initialize_database,validate_dataset
    from sweden_ubem.dataset import LocalSources
    from sweden_ubem.source_inventory import load_source_inventory
    from sweden_ubem.bundle import verify_bundle
    from sweden_ubem.status import database_status
    from sweden_ubem.ingest import initialize_sources
    from sweden_ubem.selection import process_dataset
    root=Path(os.environ['UBEM_REMOTE_ROOT'])
    state=Path(os.environ['UBEM_REMOTE_STATE'])
    package=Path(os.environ['UBEM_TRANSPORT_ROOT'])
    sources=Path(os.environ['UBEM_SOURCE_ROOT'])
    kit=Path(__file__).resolve().parent
    url=os.environ['UBEM_DATABASE_URL']
    options=conninfo_to_dict(url)
    admin=make_conninfo(**{**options,'dbname':'postgres'})
    sys.path.insert(0,str(package/'application/scripts'))

    def db_url(name):
        return make_conninfo(**{**options,'dbname':name})

    def create(name):
        if name not in {'sweden_ubem_v004_baseline','uddevalla_reference','sweden_ubem_v004_enriched'} and not name.startswith('ubem_test_remote_'):
            raise GateFailure('Database is outside this rollout')
        with psycopg.connect(admin,autocommit=True) as c:
            if not c.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():
                c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))

    def size(connection_url):
        with psycopg.connect(connection_url,options='-c default_transaction_read_only=on') as c:
            return c.execute('SELECT pg_database_size(current_database())').fetchone()[0]

    def cli(*args,environment=None):
        execute([package/'.venv/bin/sweden-ubem',*args],env=environment or os.environ.copy())

    if task=='status':
        try:
            result=database_status(url)
            publish(state/'database-status.json',result)
        except psycopg.Error:
            result={'database_unavailable':True}
    elif task=='preflight':
        if application_digest()!=config['application_digest']:
            raise GateFailure('Packaged application changed before import')
        inventory=load_source_inventory().validate(profile='linux-server',source_root=sources,verify=True)
        # Archived EPC comparison products are intentionally outside this execution bundle.
        if not inventory['valid']:
            raise GateFailure('Required source inventory validation failed')
        closure=inventory['bundle_verification']
        publish(state/'source-verification.json',closure)
        if closure.get('height_measurement_units')!=63842:
            raise GateFailure('Height measurement unit count changed')
        create('sweden_ubem_v004_baseline')
        initialize_database(url)
        environment=os.environ.copy()
        environment['UBEM_TEST_ADMIN_URL']=admin
        environment['UBEM_TEST_BACKUP_CONTAINER']='ubem-national-postgis'
        environment['PYTHONPATH']=os.pathsep.join([str(kit/'validation/tests'),str(kit/'validation'),str(package/'application/src')])
        execute([sys.executable,'-m','pytest',kit/'validation/tests','-q','-m','not integration'],env=environment,cwd=kit/'validation')
        result={'application_digest':application_digest(),'closure':closure,'tests_passed':True}
    elif task=='restore_reference':
        from compare_public_baseline import compare
        from classify_parity import classify
        retained=package/'acceptance/uddevalla'
        baseline=retained/'baseline/full-geometry'
        dump=baseline/'database.dump'
        if sha256(dump)!=config['uddevalla_dump_sha256']:
            raise GateFailure('Retained reference dump checksum changed')
        create('uddevalla_reference')
        reference=db_url('uddevalla_reference')
        with psycopg.connect(reference) as c:
            restored=c.execute("SELECT to_regclass('mart.building_facts')").fetchone()[0]
        if not restored:
            with dump.open('rb') as stream:
                execute(['docker','exec','-i','ubem-national-postgis','pg_restore','-U','ubem_owner','-d','uddevalla_reference','--exit-on-error','--no-owner','--no-acl'],stdin=stream)
        with psycopg.connect(reference,options='-c default_transaction_read_only=on') as c:
            counts=[c.execute('SELECT count(*) FROM mart.'+view+" WHERE dataset_id='uddevalla'").fetchone()[0]
                    for view in ('building_facts','building_components')]
        if counts!=[45300,47287]:
            raise GateFailure('Restored Uddevalla counts differ')
        output=state/('restored-reference-parity-'+uuid4().hex)
        parity=compare(reference,baseline,output,'uddevalla',source_root=sources)
        if not parity['equal_functional_values']:
            raise GateFailure('Restored reference differs from preserved baseline')
        result={'counts':counts,'dump_sha256':sha256(dump),'comparison':str(output)}
    elif task=='import_sources':
        import tomllib
        bundle=tomllib.loads((package/'application/config/national-source-bundle.toml').read_text())
        result=initialize_sources(url,LocalSources('linux-server',sources),bundle=bundle,batch_rows=8192,batch_bytes=64*1024**2)
    elif task=='reconcile_sources':
        from reconcile_national_sources import reconcile
        result=reconcile(url,sources,state/'national-reconciliation.json')
        with psycopg.connect(url,options='-c default_transaction_read_only=on') as c:
            rows=c.execute('SELECT (SELECT count(*) FROM raw_epc.before_v260_row)+(SELECT count(*) FROM raw_epc.after_v260_row)').fetchone()[0]
        if rows!=1883795:raise GateFailure('EPC source rows differ from transported source receipt')
        result['epc_source_rows']=rows
    elif task=='uddevalla_acceptance':
        from validate_reimplementation import validate
        output=state/('uddevalla-acceptance-'+uuid4().hex)
        # Use a named kernel so notebook execution cannot select an unrelated Python.
        notebook=package/'application/notebooks/explore_uddevalla.ipynb'
        import nbformat
        nb=nbformat.read(notebook,as_version=4)
        kernel=nb.metadata.get('kernelspec',{}).get('name','python3')
        # The acceptance script selects python3 explicitly; install that name for this user.
        from types import SimpleNamespace
        code=validate(SimpleNamespace(database_url=url,baseline=package/'acceptance/uddevalla/baseline/full-geometry',
                                      output=output,source_root=sources,importer_pid=None))
        if code!=0:
            raise GateFailure('Uddevalla has unexplained differences')
        result=json.loads((output/'status.json').read_text())
        with psycopg.connect(url,options='-c default_transaction_read_only=on') as c:
            counts=[c.execute('SELECT count(*) FROM mart.'+view+" WHERE dataset_id='uddevalla'").fetchone()[0]
                    for view in ('building_facts','building_components')]
        if counts!=[45300,47287]:
            raise GateFailure('Uddevalla output cohort counts differ')
        result.update(counts=counts,output=str(output))
    elif task=='processing_capacity':
        measured=[]
        for municipality in ('1480','1784','0180'):
            check_capacity(root,config['reserve_bytes'])
            before=size(url);started=time.monotonic()
            processed=process_dataset(url,dataset='capacity_'+municipality,municipality=municipality,assessment_date='2026-10-12')
            if not processed['validation']['valid']:
                raise GateFailure('Representative municipality validation failed')
            after=size(url)
            with psycopg.connect(url,options='-c default_transaction_read_only=on') as c:
                cohort=c.execute('SELECT count(*) FROM mart.building_facts WHERE dataset_id=%s',('capacity_'+municipality,)).fetchone()[0]
            measured.append(dict(municipality=municipality,seconds=time.monotonic()-started,
                                 added_database_bytes=max(0,after-before),building_rows=cohort))
        worst=max(r['added_database_bytes']/max(1,r['building_rows']) for r in measured)
        expected_derived=int(worst*9055408*1.35)
        # Preserve baseline and enriched DB plus a full restore-tested backup and transient work.
        additional=expected_derived*3+size(url)*2+32*1024**3+config['reserve_bytes']
        available=shutil.disk_usage(root).free
        result=dict(samples=measured,projected_derived_bytes=expected_derived,
                    required_additional_bytes=additional,available_bytes=available,accepted=additional<available)
        publish(state/'processing-capacity.json',result)
        if not result['accepted']:
            raise GateFailure('Measured national processing/backup capacity exceeds available storage')
    elif task=='process_national':
        result=process_dataset(url,dataset='sweden',municipality=None,assessment_date='2026-10-12')
        if not result['validation']['valid']:
            raise GateFailure('National processing validation failed')
    else:
        from remote_enrichment import execute_task
        result=execute_task(task,config)
    publish(state/(task+'-result.json'),result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('task')
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(main(args.task,json.loads(args.config.read_text())),default=str),flush=True)
    except Exception as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr)
        raise SystemExit(78)
