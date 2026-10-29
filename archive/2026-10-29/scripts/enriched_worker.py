"""Validate and build a fresh enriched backing snapshot; never upgrade baseline."""
import argparse
import json
import os
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace
from uuid import uuid4

from remote_pipeline import GateFailure,publish


def run(args):
    sys.path.insert(0,str(args.application/'scripts'))
    from sweden_ubem.database import initialize_database,application_digest
    from sweden_ubem.dataset import LocalSources
    from sweden_ubem.ingest import initialize_sources
    from sweden_ubem.bundle import verify_bundle
    from sweden_ubem.selection import process_dataset
    from reconcile_national_sources import reconcile
    from validate_reimplementation import validate
    url=os.environ['UBEM_DATABASE_URL']
    bundle=tomllib.loads((args.application/'config/national-source-bundle.toml').read_text())
    closure=verify_bundle(args.source_root)
    if not closure['height_production_promotable']:
        raise GateFailure('Enriched height evidence has not passed its publication contract')
    publish(args.state/'enriched-source-verification.json',closure)
    initialize_database(url)
    initialize_sources(url,LocalSources('linux-server',args.source_root),bundle=bundle,batch_rows=8192,batch_bytes=64*1024**2)
    reconcile(url,args.source_root,args.state/'enriched-national-reconciliation.json')
    acceptance=args.state/('enriched-uddevalla-'+uuid4().hex)
    if validate(SimpleNamespace(database_url=url,baseline=Path(os.environ['UBEM_TRANSPORT_ROOT'])/'acceptance/uddevalla/baseline/full-geometry',
                  output=acceptance,source_root=args.source_root,importer_pid=None)):
        raise GateFailure('Enriched Uddevalla has unexplained differences; inspect keyed outputs')
    result=process_dataset(url,dataset='sweden',municipality=None,assessment_date='2026-10-12')
    if not result['validation']['valid']:
        raise GateFailure('Enriched national processing validation failed')
    import psycopg
    with psycopg.connect(url,options='-c default_transaction_read_only=on') as c:
        selected={r[0] for r in c.execute("SELECT area_code FROM context.area WHERE dataset_id='national' AND area_type='municipality'")}
        if len(selected)!=290:raise GateFailure('Enriched national municipality coverage is incomplete')
        areas,income=c.execute("SELECT count(*),count(*) FILTER (WHERE EXISTS(SELECT 1 FROM context.observation o WHERE o.dataset_id=a.dataset_id AND o.area_type=a.area_type AND o.area_code=a.area_code AND o.indicator_id LIKE '%%income%%')) FROM context.area a WHERE a.dataset_id='national' AND a.area_type='regso'").fetchone()
        if not areas or areas!=income:raise GateFailure('Enriched national income coverage is incomplete')
    result.update(application_digest=application_digest(),municipality_coverage=len(selected),income_regso_areas=income,
                  uddevalla_acceptance=str(acceptance))
    publish(args.state/'enriched-processing.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--application',type=Path,required=True)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--state',type=Path,required=True)
    try:print(json.dumps(run(parser.parse_args()),default=str))
    except Exception as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr)
        raise SystemExit(78)
