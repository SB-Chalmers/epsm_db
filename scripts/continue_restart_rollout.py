"""Continue the approved sequential gates after the active baseline benchmark."""
import json
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from sweden_ubem.database import application_digest
from sweden_ubem.asset_operations import publish_json

ROOT=Path(__file__).resolve().parents[1]
RESTART=ROOT/'output/restart-2026-10-13'
BENCH=RESTART/'benchmarks'
DATABASE='postgresql://ubem_owner:ubem-local-only@127.0.0.1:55433/sweden_ubem_v004'
SOURCE=ROOT/'data/raw/national-2026-10-13'


def stage(name,**details):
    value=dict(stage=name,pid=os.getpid(),**details)
    publish_json(RESTART/'rollout-gates.json',value)
    print(json.dumps(value),flush=True)


def run(command,log):
    with Path(log).open('w') as stream:
        result=subprocess.run(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT)
    if result.returncode:raise RuntimeError(f'Gate failed; inspect {log}')


def continue_rollout(view_correction=None):
    control=RESTART/'national-run-control.json'
    if control.exists() and json.loads(control.read_text()).get('paused'):
        raise RuntimeError('National ingestion is paused by the user; explicit resume is required')
    environment=json.loads((BENCH/'environment.json').read_text())
    expected=environment['application_digest']
    old=BENCH/'old-1048576-final.json'
    stage('waiting_for_active_baseline_benchmark')
    while not old.exists():
        if application_digest()!=expected:raise RuntimeError('Implementation changed during rollout')
        processes=subprocess.check_output(['ps','ax','-o','command='],text=True)
        if 'loader_benchmark_worker.py --implementation old' not in processes:
            raise RuntimeError('Baseline benchmark exited without successful result')
        time.sleep(30)
    if not json.loads(old.read_text()).get('finished'):raise RuntimeError('Baseline benchmark incomplete')
    while 'loader_benchmark_worker.py --implementation old' in subprocess.check_output(['ps','ax','-o','command='],text=True):
        time.sleep(2)
    if view_correction:
        correction=json.loads(Path(view_correction).read_text())
        expected=correction['corrected_code_digest']
        if not correction.get('accepted') or not json.loads((BENCH/'new-1048576-final.json').read_text()).get('finished'):
            raise RuntimeError('Completed benchmark and verified view correction are required')
        stage('accepting_verified_view_correction',receipt=str(view_correction))
    else:
        stage('benchmarking_replacement_at_scale')
        run([sys.executable,'scripts/loader_benchmark_worker.py','--implementation','new',
             '--code-root',str(BENCH/'new-code'),'--sample',str(BENCH/'samples/1048576'),
             '--output',str(BENCH/'new-1048576-final.json'),'--dump-facts'],BENCH/'new-1048576-final.log')
    if application_digest()!=expected:raise RuntimeError('Implementation changed during measured acceptance')
    stage('evaluating_performance_and_capacity')
    report=RESTART/'performance-acceptance.json'
    acceptance=[sys.executable,'scripts/accept_loader_benchmark.py','--benchmarks',str(BENCH),
                '--source-root',str(SOURCE),'--output',str(report)]
    if view_correction:acceptance+=['--view-correction',str(view_correction)]
    run(acceptance,RESTART/('performance-acceptance.corrected.log' if view_correction else 'performance-acceptance.log'))
    stage('national_import_and_uddevalla_acceptance')
    env=os.environ.copy();env['UBEM_CAPACITY_GUARD_PATH']=str(ROOT)
    env['UBEM_MIN_FREE_BYTES']=str(15*1024**3)
    with (RESTART/'national-rebuild.log').open('w') as stream:
        result=subprocess.run([sys.executable,'scripts/run_reimplementation.py',
          '--database-url',DATABASE,'--source-bundle','config/national-source-bundle.toml',
          '--source-root',str(SOURCE),'--performance-report',str(report),
          '--baseline',str(ROOT/'output/retained/uddevalla-2026-10-12/baseline/full-geometry'),
          '--output',str(RESTART/'national-build')],cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT)
    if result.returncode:raise RuntimeError('National or Uddevalla gate failed; inspect national-rebuild.log')
    stage('source_completion_and_uddevalla_acceptance_passed',
          national_publication='blocked by existing height coverage/representation and EPSM preflight gates')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--view-correction',type=Path)
    args=parser.parse_args()
    try:continue_rollout(args.view_correction)
    except BaseException as error:
        stage('attention_required',error=str(error));raise
