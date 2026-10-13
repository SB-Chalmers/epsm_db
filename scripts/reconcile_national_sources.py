"""Independent count gate; never substitutes for Uddevalla or publication acceptance."""
import argparse
import json
from pathlib import Path
import psycopg
from sweden_ubem.asset_operations import publish_json

EXPECTED={'buildings':9055408,'components':9464839,'lineage_rows':9465504}

def reconcile(url,root,output):
    manifest=json.loads((Path(root)/'prepared/components/manifest.json').read_text())
    if manifest['component_count']!=EXPECTED['components'] or manifest['source_row_count']!=EXPECTED['lineage_rows']:
        raise ValueError('Source manifest differs from independent national targets')
    with psycopg.connect(url,options='-c default_transaction_read_only=on') as c:
        source=c.execute("SELECT sealed FROM catalog.dataset WHERE dataset_id='national' AND dataset_kind='national_source'").fetchone()
        if not source or not source[0]:raise ValueError('National source is not complete')
        buildings=c.execute("SELECT count(*) FROM stock.building WHERE dataset_id='national'").fetchone()[0]
        components,cohort=c.execute("SELECT count(*),count(DISTINCT building_id) FROM stock.component_data WHERE dataset_id='national'").fetchone()
        lineage=c.execute("SELECT count(*) FROM stock.component_lineage l JOIN stock.component_data d USING(component_key) WHERE d.dataset_id='national'").fetchone()[0]
        constraints=c.execute("SELECT connamespace::regnamespace::text,conname FROM pg_constraint WHERE NOT convalidated AND connamespace IN (SELECT oid FROM pg_namespace WHERE nspname IN ('catalog','stock','evidence','scope','context','quality'))").fetchall()
        layers=c.execute("SELECT layer,loading_status FROM catalog.dataset_layer WHERE dataset_id='national' ORDER BY layer").fetchall()
    observed=dict(buildings=buildings,components=components,lineage_rows=lineage)
    result=dict(expected=EXPECTED,observed=observed,component_building_cohort=cohort,
                constraints_unvalidated=constraints,layers=layers,
                source_completion_passed=observed==EXPECTED and cohort==buildings and not constraints and all(s=='loaded' for _,s in layers),
                uddevalla_acceptance='separate gate',national_publication='separate gate')
    publish_json(output,result)
    if not result['source_completion_passed']:raise ValueError('Independent national reconciliation failed')
    return result

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--database-url',required=True);p.add_argument('--source-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(reconcile(a.database_url,a.source_root,a.output)))
