"""Build and read-back verify the portable national execution/acceptance transport."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import time

import pyarrow.parquet as pq

from sweden_ubem.asset_operations import publish_json, rename_exclusive, safe_relative
from sweden_ubem.bundle import translated_reference, verify_bundle
from sweden_ubem.dataset import LocalSources, municipality_rows
from sweden_ubem.context import project_scb_snapshot
from sweden_ubem.hashing import sha256_file
from sweden_ubem.scb import validate_scb_snapshot
from accept_loader_benchmark import epc_input_budget

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'output/remote-transfer'
EXCLUDED = ('boverket/epc/comparison/',)


def state(stage, **details):
    value = dict(stage=stage, timestamp=datetime.now(timezone.utc).isoformat(), **details)
    publish_json(OUTPUT / 'transport-progress.json', value)
    print(json.dumps(value), flush=True)


def identity(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def link_file(source, destination):
    if source.is_symlink() or not source.is_file():
        raise ValueError('Transport requires an ordinary file: ' + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, destination)


VERIFIER = '''"""Verify transported files before installing or importing anything."""
import hashlib,json,sys
from pathlib import Path
root=Path(__file__).resolve().parent
manifest=json.loads((root/'transport-file-manifest.json').read_text())
cache={}
for n,row in enumerate(manifest['files'],1):
    relative=Path(row['path'])
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe transport path')
    p=root/relative
    if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root):raise ValueError('Missing or escaping file: '+str(relative))
    s=p.stat();key=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
    if key not in cache:
        h=hashlib.sha256()
        with p.open('rb') as f:
            for b in iter(lambda:f.read(8*1024**2),b''):h.update(b)
        t=p.stat()
        if key!=(t.st_dev,t.st_ino,t.st_size,t.st_mtime_ns):raise ValueError('File changed during verification')
        cache[key]=h.hexdigest()
    if s.st_size!=row['bytes'] or cache[key]!=row['sha256']:raise ValueError('Checksum mismatch: '+str(relative))
    if n%10000==0:print('Verified',n,'files',flush=True)
print('All',len(manifest['files']),'transport file occurrences verified')
'''


README = '''# National execution transport

This package contains the current national source inputs, complete height
measurement/supporting evidence and manifests, the corrected tested application,
operational scripts and the retained Uddevalla acceptance baseline. Historical
EPC comparison releases remain preserved on the Mac and are excluded here.
Source documents and historical manifests retain their original bytes. A generated
subset inventory preserves the selected original locations and logical aliases.

Verify before use:

    python3 verify_transport.py

On the destination, install Python 3.12 and PostgreSQL 17/PostGIS 3.5, create a
fresh database, then install the application with the measured dependency pins:

    python3.12 -m venv .venv
    .venv/bin/pip install -c requirements-tested.txt -e './application[notebook]'
    export UBEM_SOURCE_ROOT="$PWD/sources"
    export UBEM_DATABASE_URL='postgresql://USER:PASSWORD@127.0.0.1:PORT/DATABASE'
    export UBEM_CAPACITY_GUARD_PATH='/absolute/path/to/the/database/filesystem'
    export UBEM_MIN_FREE_BYTES=16106127360

Verify bundle closure and required sources, then import once:

    .venv/bin/sweden-ubem source validate --verify --source-root "$UBEM_SOURCE_ROOT"
    .venv/bin/sweden-ubem db init --source-bundle application/config/national-source-bundle.toml --source-root "$UBEM_SOURCE_ROOT"
    .venv/bin/sweden-ubem db status
    .venv/bin/python application/scripts/reconcile_national_sources.py --database-url "$UBEM_DATABASE_URL" --source-root "$UBEM_SOURCE_ROOT" --output national-reconciliation.json

Reproduce Uddevalla and compare its full-geometry baseline before broader release:

    .venv/bin/python application/scripts/validate_reimplementation.py --database-url "$UBEM_DATABASE_URL" --source-root "$UBEM_SOURCE_ROOT" --baseline "$PWD/acceptance/uddevalla/baseline/full-geometry" --output "$PWD/remote-uddevalla-acceptance"

The acceptance script disconnects the raw root while checking ordinary consumers.
After acceptance, process national or individual municipal selections:

    .venv/bin/sweden-ubem dataset process --dataset sweden --national
    .venv/bin/sweden-ubem dataset validate --dataset sweden

The available RegSO/income snapshots cover Uddevalla only. Existing height
coverage/representation and EPSM preflight publication blockers remain. Packaging
does not acquire missing nationwide statistics or establish full national
publication or simulation acceptance. Dependencies and database software are
downloaded on the destination; this package contains no Mac virtual environment,
PostgreSQL runtime directory, national database dump or benchmark sample databases.
The retained Uddevalla dump is included only as a restoration/acceptance reference.

The measured benchmark is included as a reference. The remote environment and
full-national processing still require their own capacity and acceptance gates.
Local national ingestion remains paused and its partial database was cleared.
'''


def build(source_root, zstd):
    source_root = Path(source_root).resolve()
    package = OUTPUT / 'national-execution-2026-10-13'
    archive = OUTPUT / 'national-execution-2026-10-13.tar.zst'
    if package.exists() or archive.exists():
        raise FileExistsError('Transport destination exists; inspect it rather than overwriting')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    package.mkdir()
    sources = package / 'sources'
    inventory_path = source_root / 'manifests/file-inventory.json'
    original = json.loads(inventory_path.read_text())
    selected = [r for r in original['files'] if not any(r['path'].startswith(p) for p in EXCLUDED)]
    bypath = {}
    for row in selected:
        relative = str(safe_relative(row['path']))
        if relative in bypath and (bypath[relative]['sha256'], bypath[relative]['bytes']) != (row['sha256'], row['bytes']):
            raise ValueError('Conflicting selected source occurrences')
        bypath[relative] = row
    state('staging_required_sources', selected_files=len(bypath), excluded_prefixes=EXCLUDED)
    for n, relative in enumerate(sorted(bypath), 1):
        link_file(source_root / relative, sources / relative)
        if n % 10000 == 0:
            state('staging_required_sources', files_staged=n, selected_files=len(bypath))
    (sources / 'manifests').mkdir()
    subset = dict(files=selected, unique_assets=len({r['sha256'] for r in selected}),
                  physical_deduplication='bundle-internal hard links',
                  derived_from_inventory_sha256=sha256_file(inventory_path),
                  transport_scope='current national inputs and required supporting provenance')
    publish_json(sources / 'manifests/file-inventory.json', subset)
    for name in ('height-assets.json', 'inventory-paths.json'):
        link_file(source_root / 'manifests' / name, sources / 'manifests' / name)
    selection = dict(excluded_prefixes=EXCLUDED, excluded_files=len({r['path'] for r in original['files']} - set(bypath)),
                     original_inventory_sha256=subset['derived_from_inventory_sha256'],
                     original_sources_preserved=True)
    publish_json(sources / 'manifests/transport-selection.json', selection)

    frozen = ROOT / 'output/restart-2026-10-13/benchmarks/new-code-lineage-fixed'
    application = package / 'application'
    shutil.copytree(frozen, application, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Verified source-root resolution fixes; preserve the measured loader otherwise.
    for name in ('scb.py', 'context.py'):
        shutil.copy2(ROOT / 'src/sweden_ubem' / name, application / 'src/sweden_ubem' / name)
    (application / 'scripts').mkdir(exist_ok=True)
    for name in ('reconcile_national_sources.py', 'validate_reimplementation.py',
                 'compare_public_baseline.py', 'classify_parity.py'):
        shutil.copy2(ROOT / 'scripts' / name, application / 'scripts' / name)
    (application / 'notebooks').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'notebooks/explore_uddevalla.ipynb', application / 'notebooks/explore_uddevalla.ipynb')
    retained = ROOT / 'output/retained/uddevalla-2026-10-12'
    acceptance = package / 'acceptance/uddevalla'
    for p in retained.rglob('*'):
        if p.is_file():
            link_file(p, acceptance / p.relative_to(retained))
    (package / 'README-TRANSPORT.md').write_text(README)
    (package / 'verify_transport.py').write_text(VERIFIER)
    distributions = ('psycopg', 'psycopg-binary', 'pyarrow', 'pyogrio', 'shapely', 'geopandas',
                     'pandas', 'pyproj', 'numpy', 'matplotlib', 'nbclient', 'nbformat', 'ipykernel', 'setuptools')
    (package / 'requirements-tested.txt').write_text(''.join(f'{d}=={version(d)}\n' for d in distributions))
    shutil.copy2(ROOT / 'output/restart-2026-10-13/performance-acceptance.json', package / 'reference-benchmark.json')

    state('verifying_every_selected_file')
    cache = {}
    files = []
    total_bytes = 0
    started = last = time.monotonic()
    for p in sorted(package.rglob('*')):
        if not p.is_file():
            continue
        if p.is_symlink():
            raise ValueError('Transport symlink is not allowed')
        key = identity(p)
        if key not in cache:
            cache[key] = sha256_file(p)
            total_bytes += key[2]
        if key != identity(p):
            raise ValueError('Transport asset changed during verification')
        relative = str(p.relative_to(package))
        if relative.startswith('sources/'):
            expected = bypath.get(relative[len('sources/'):])
            if expected and (cache[key], key[2]) != (expected['sha256'], expected['bytes']):
                raise ValueError('Source differs from pinned inventory: ' + relative)
        files.append(dict(path=relative, bytes=key[2], sha256=cache[key]))
        if time.monotonic() - last >= 30:
            state('verifying_every_selected_file', files_verified=len(files), unique_bytes_verified=total_bytes)
            last = time.monotonic()

    import sweden_ubem.bundle as bundle
    actual_hash = bundle.sha256_file
    def checked_hash(path):
        key = identity(Path(path))
        return cache[key] if key in cache else actual_hash(path)
    bundle.sha256_file = checked_hash
    try:
        source_verification = verify_bundle(sources)
    finally:
        bundle.sha256_file = actual_hash
    manifest = json.loads((sources / 'prepared/components/manifest.json').read_text())
    def source_hash(relative):
        return cache[identity(sources / relative)]
    assert source_hash('lantmateriet/buildings/byggnad_riks.gpkg') == manifest['source_sha256']
    assert source_hash('prepared/components/source-lineage.parquet') == manifest['source_lineage_sha256']
    for name, digest in manifest['partition_sha256'].items():
        assert source_hash('prepared/components/components/' + name) == digest
    height_assets = json.loads((sources / 'manifests/height-assets.json').read_text())['assets']
    declared = {(r['evidence_root'], r['original_path'], r['sha256']) for r in height_assets}
    referenced = set()
    catalogue = sources / 'prepared/heights/component-stage/handoff/component-height-catalogue.parquet'
    for batch in pq.ParquetFile(catalogue).iter_batches(batch_size=32768, columns=['evidence_root', 'evidence_record_path', 'evidence_record_sha256']):
        for row in batch.to_pylist():
            if row['evidence_record_path']:
                referenced.add((row['evidence_root'], row['evidence_record_path'], row['evidence_record_sha256']))
    if referenced != declared:
        raise ValueError('Catalogue measurement references differ from transported asset mappings')
    local = LocalSources(root=sources)
    missing = [r.key for r in local.inventory.sources if r.status in ('required', 'staging_only') and not local.path(r.key).exists()]
    if missing:
        raise ValueError('Required source entries absent: ' + str(missing))
    snapshot = local.path('scb_snapshot_uddevalla')
    context = validate_scb_snapshot(snapshot)
    municipality = municipality_rows(snapshot, '1485')
    projected = project_scb_snapshot(snapshot, municipality.geometry.iloc[0], municipality='1485', include_population=False)
    events = sources / 'site-events/assertions/uddevalla-epc-site-events.json'
    for e in json.loads(events.read_text())['events']:
        translated_reference(events, e['source_document'], source_root=sources, expected_sha256=e['source_document_sha256'])
    epc = epc_input_budget(local.path('epc_raw_root'))
    assert epc['source_rows'] == 1883795
    with sqlite3.connect(local.path('building_registry_locator').as_uri() + '?mode=ro', uri=True) as c:
        registry_rows = c.execute('SELECT count(*) FROM registry_buildings').fetchone()[0]
    environment = dict(os.environ, PYTHONPATH=str(application / 'src'), PYTHONDONTWRITEBYTECODE='1')
    code_digest = subprocess.check_output([str(ROOT / '.venv/bin/python'), '-c',
        'from sweden_ubem.database import application_digest; print(application_digest())'], env=environment, text=True).strip()
    audit = dict(source_verification=source_verification, epc_source_rows=epc['source_rows'], epc_files=len(epc['parsed_files']),
                 height_catalogue_dependency_closure=True, scb_ready=context['ready'],
                 scb_projected_areas=len(projected['areas']), registry_rows=registry_rows,
                 required_source_entries_present=True, original_source_bytes_unchanged=True,
                 application_digest=code_digest, verification_seconds=time.monotonic() - started)
    publish_json(package / 'required-input-audit.json', audit)
    p = package / 'required-input-audit.json'
    files.append(dict(path=p.name, bytes=p.stat().st_size, sha256=sha256_file(p)))
    document = dict(format='national-transport-v1', files=files, required_input_audit=audit, selection=selection)
    publish_json(package / 'transport-file-manifest.json', document)
    expected = {r['path']:r for r in files}
    p = package / 'transport-file-manifest.json'
    expected[p.name] = dict(bytes=p.stat().st_size, sha256=sha256_file(p))
    state('compressing_verified_transport', file_occurrences=len(expected), unique_input_bytes=total_bytes,
          compression='zstd level 6, four threads')
    temporary = archive.with_suffix(archive.suffix + '.partial')
    if temporary.exists():
        raise FileExistsError('Interrupted archive exists; inspect before replacing')
    with temporary.open('xb') as output:
        process = subprocess.Popen([zstd, '-6', '-T4', '-c'], stdin=subprocess.PIPE, stdout=output)
        try:
            with tarfile.open(fileobj=process.stdin, mode='w|', format=tarfile.PAX_FORMAT) as tar:
                for relative in sorted(expected):
                    p = package / relative
                    tar.add(p, arcname=package.name + '/' + relative, recursive=False)
            process.stdin.close()
            if process.wait():
                raise RuntimeError('Compression failed')
        finally:
            if process.poll() is None:
                process.terminate(); process.wait()
    state('verifying_compressed_archive_contents', compressed_bytes=temporary.stat().st_size)
    process = subprocess.Popen([zstd, '-d', '-c', str(temporary)], stdout=subprocess.PIPE)
    seen = {}
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as tar:
            for member in tar:
                name = str(safe_relative(member.name))
                relative = str(Path(name).relative_to(package.name))
                if relative not in expected or relative in seen:
                    raise ValueError('Unexpected or duplicate archive member: ' + relative)
                if member.islnk():
                    target = str(Path(str(safe_relative(member.linkname))).relative_to(package.name))
                    if target not in seen:
                        raise ValueError('Unverified hard-link target')
                    digest, size = seen[target]
                elif member.isfile():
                    h = hashlib.sha256()
                    size = 0
                    with tar.extractfile(member) as stream:
                        for block in iter(lambda:stream.read(8*1024**2), b''):
                            h.update(block); size += len(block)
                    digest = h.hexdigest()
                else:
                    raise ValueError('Unsupported archive member type')
                if (digest, size) != (expected[relative]['sha256'], expected[relative]['bytes']):
                    raise ValueError('Archive payload checksum differs: ' + relative)
                seen[relative] = (digest, size)
        if process.wait():
            raise RuntimeError('Archive decompression failed')
    finally:
        if process.poll() is None:
            process.terminate(); process.wait()
    if set(seen) != set(expected):
        raise ValueError('Archive closure is incomplete')
    rename_exclusive(temporary, archive)
    digest = sha256_file(archive)
    archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '  ' + archive.name + '\n')
    result = dict(archive=str(archive), archive_sha256=digest, compressed_bytes=archive.stat().st_size,
                  unique_input_bytes=total_bytes, archived_file_occurrences=len(seen),
                  compressed_payloads_verified=True, **audit)
    publish_json(OUTPUT / 'compressed-transport-result.json', result)
    state('transport_ready', **result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=ROOT / 'data/raw/national-2026-10-13')
    parser.add_argument('--zstd', default=shutil.which('zstd'))
    args = parser.parse_args()
    if not args.zstd:
        raise RuntimeError('zstd executable is required')
    try:
        build(args.source_root, args.zstd)
    except BaseException as error:
        state('attention_required', error=str(error)); raise
