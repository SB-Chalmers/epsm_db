"""Freeze the remote supervisor, compatible validation suite and height/Core tools."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'output/remote-execution'
FILES=('remote_pipeline.py','remote_tasks.py','remote_enrichment.py','enriched_worker.py')
TESTS=('asset_operations','components','lm_xml','linkage','comparison','epc_assessment',
       'compact_pipeline','national_pipeline','height_staging','height_release','physical_assessment',
       'postgis_integration','scb','source_inspection','v001','remote_pipeline')


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def copy_tree(source,destination):
    shutil.copytree(source,destination,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.DS_Store','*.egg-info'))


def build():
    kit=OUTPUT/'kit-v1'
    if kit.exists():raise FileExistsError('Existing execution kit must not be overwritten')
    kit.mkdir(parents=True)
    for name in FILES:shutil.copy2(ROOT/'scripts'/name,kit/name)
    for name in ('compose.yaml','bootstrap-admin.sh'):shutil.copy2(ROOT/'scripts/remote_runtime'/name,kit/name)
    shutil.copy2(ROOT/'output/remote-transfer/RUN-REMOTE.md',kit/'RUN-REMOTE.md')
    archive=ROOT/'output/remote-transfer/national-execution-2026-10-13'
    frozen=archive/'application'
    validation=kit/'validation';(validation/'tests').mkdir(parents=True)
    for name in TESTS:shutil.copy2(ROOT/'tests'/('test_'+name+'.py'),validation/'tests'/('test_'+name+'.py'))
    shutil.copy2(ROOT/'tests/fixture_factory.py',validation/'tests/fixture_factory.py')
    copy_tree(ROOT/'tests/fixtures',validation/'tests/fixtures')
    copy_tree(frozen/'sql',validation/'sql')
    copy_tree(frozen/'config',validation/'config')
    copy_tree(frozen/'notebooks',validation/'notebooks')
    (validation/'scripts').mkdir()
    for name in ('classify_parity.py','compare_public_baseline.py'):
        shutil.copy2(frozen/'scripts'/name,validation/'scripts'/name)
    for name in FILES:shutil.copy2(kit/name,validation/'scripts'/name)
    (validation/'pyproject.toml').write_text('[tool.pytest.ini_options]\npythonpath=[".","tests"]\nmarkers=["integration: requires external data", "postgis: requires disposable database"]\n')
    overlay=kit/'enriched-overlay';overlay.mkdir()
    # Only these audited portability/closure changes enter the enriched snapshot.
    text=(frozen/'src/sweden_ubem/ingest.py').read_text()
    old='        keys = self.bundle.get("scb_snapshot_keys", ["scb_snapshot_uddevalla"])\n        snapshots = [self.sources.path(k) for k in keys]'
    new='        from .snapshot_paths import snapshot_paths\n        snapshots = snapshot_paths(self.sources, self.bundle)'
    if old not in text:raise ValueError('Frozen SCB loader shape changed')
    text=text.replace(old,new).replace('    from .dataset import LocalSources\n','    from .dataset import LocalSources\n    from .snapshot_paths import snapshot_paths\n')
    text=text.replace('                LocalSources,\n','                LocalSources,\n                snapshot_paths,\n')
    (overlay/'ingest.py').write_text(text)
    for name in ('bundle.py','config.py','snapshot_paths.py'):shutil.copy2(ROOT/'src/sweden_ubem'/name,overlay/name)
    shutil.copy2(ROOT/'tests/test_snapshot_paths.py',kit/'test_snapshot_paths.py')
    heights=Path('/Users/ssanjay/GitHub/footprint_height_extraction')
    tools=kit/'height-tooling'
    for directory in ('src','scripts','tests','inputs'):
        copy_tree(heights/directory,tools/directory)
    shutil.copy2(heights/'pyproject.toml',tools/'pyproject.toml')
    pins=json.loads((tools/'inputs/component-pipeline-code.json').read_text())
    for name,expected in pins['sha256'].items():
        if digest(tools/name)!=expected:raise ValueError('Height code does not match tested pin: '+name)
    core=Path('/Users/ssanjay/GitHub/epsm_core')
    copy_tree(core/'src',kit/'epsm-core/src')
    shutil.copy2(core/'pyproject.toml',kit/'epsm-core/pyproject.toml')
    receipt=json.loads((ROOT/'output/remote-transfer/compressed-transport-result.json').read_text())
    config=dict(root='/home/csd-lab/ubem',release_id='national-execution-2026-10-13',
                remote_archive='/home/csd-lab/ubem/incoming/national-execution-2026-10-13.tar.zst.incoming',
                archive_bytes=receipt['compressed_bytes'],archive_sha256=receipt['archive_sha256'],
                uncompressed_bytes=receipt['uncompressed_payload_bytes'],file_occurrences=receipt['archived_file_occurrences'],
                application_digest=receipt['application_digest'],reserve_bytes=50*1024**3,
                image='postgis/postgis:17-3.5@sha256:8dfee83d8bd4c2873dc4a233c13ba2799a44f2edb16a0552d58715917fac32ba',
                uddevalla_dump_sha256='fc3226a700579ee54d897ca3b76dbf6bbce05f8ded5a3b4a34dea14d03ff22ab')
    (kit/'config.json').write_text(json.dumps(config,indent=2)+'\n')
    unit='''[Unit]\nDescription=UBEM verified remote national pipeline\nAfter=network-online.target\n\n[Service]\nType=simple\nWorkingDirectory=/home/csd-lab/ubem/control/kit-v1\nExecStart=/usr/bin/python3 /home/csd-lab/ubem/control/kit-v1/remote_pipeline.py run --config /home/csd-lab/ubem/control/kit-v1/config.json\nRestart=on-failure\nRestartSec=60\nRestartPreventExitStatus=78\nKillMode=control-group\nTimeoutStopSec=90\n\n[Install]\nWantedBy=default.target\n'''
    (kit/'ubem-remote.service').write_text(unit)
    files=[dict(path=str(p.relative_to(kit)),bytes=p.stat().st_size,sha256=digest(p)) for p in sorted(kit.rglob('*')) if p.is_file()]
    (kit/'kit-manifest.json').write_text(json.dumps(dict(files=files),indent=2)+'\n')
    transport=OUTPUT/'execution-kit-v1.tar.gz'
    with tarfile.open(transport,'w:gz') as tar:tar.add(kit,arcname='kit-v1')
    result=dict(archive=str(transport),sha256=digest(transport),bytes=transport.stat().st_size,
                kit_manifest_sha256=digest(kit/'kit-manifest.json'),files=len(files))
    (OUTPUT/'execution-kit-v1.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':build()
