"""Verify a display-only lineage correction against the full measured cohort."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from loader_benchmark_worker import SampleSources
from sweden_ubem.asset_operations import publish_json
from sweden_ubem.database import application_digest, initialize_database
from sweden_ubem.hashing import sha256_file
from sweden_ubem.ingest import initialize_sources, source_projection_digest


def verify(base, output):
    base = Path(base).resolve()
    old_path = base / 'old-1048576-final.json'
    new_path = base / 'new-1048576-final.json'
    old = json.loads(old_path.read_text())
    new = json.loads(new_path.read_text())
    info = conninfo_to_dict(os.environ['UBEM_TEST_ADMIN_URL'])
    info['dbname'] = 'postgres'
    admin = make_conninfo(**info)
    name = 'ubem_test_lineage_correction_' + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    info['dbname'] = name
    url = make_conninfo(**info)
    started = time.monotonic()
    try:
        initialize_database(url)
        print(json.dumps({'stage': 'loading_same_full_geometry_cohort', 'database': name}), flush=True)
        sources = SampleSources(base / 'samples/1048576')
        initialize_sources(url, sources, layers=['geometry'])
        print(json.dumps({'stage': 'verifying_corrected_public_lineage'}), flush=True)
        counts = Counter()
        with psycopg.connect(url, options='-c default_transaction_read_only=on') as c:
            with c.cursor(name='lineage_correction') as cursor:
                cursor.execute("SELECT jsonb_build_object('component_id',component_id,'source_fid',source_fid,'disposition',disposition,'original_values',original_values) FROM stock.component_source_feature")
                for (value,) in cursor:
                    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
                    counts[hashlib.sha256(encoded).digest()] += 1
        digest = hashlib.sha256()
        for row_digest, multiplicity in sorted(counts.items()):
            digest.update(row_digest)
            digest.update(multiplicity.to_bytes(8, 'big'))
        observed = dict(rows=sum(counts.values()), multiset_sha256=digest.hexdigest())
        differences = [k for k in old['facts'] if old['facts'][k] != new['facts'].get(k)]
        report = dict(
            original_code_root=str(base / 'new-code'),
            corrected_code_root=str(base / 'new-code-lineage-fixed'),
            original_code_digest=json.loads((base / 'environment.json').read_text())['application_digest'],
            corrected_code_digest=application_digest(),
            source_projection_digest=source_projection_digest(),
            old_benchmark_sha256=sha256_file(old_path),
            new_benchmark_sha256=sha256_file(new_path),
            source_lineage_sha256=sha256_file(sources.components() / 'source-lineage.parquet'),
            original_lineage=new['facts']['lineage'],
            expected_lineage=old['facts']['lineage'],
            corrected_lineage=observed,
            original_differing_groups=differences,
            verification_seconds=time.monotonic() - started,
            accepted=observed == old['facts']['lineage'] and differences == ['lineage'],
            explanation="The sole change casts the lineage view's original_values.object_version to text. Loader code, stored facts, physical schema and indexes are unchanged. Original measured results are retained verbatim; this full-cohort check supplements correctness acceptance.",
        )
        publish_json(output, report)
        if not report['accepted']:
            raise ValueError('Corrected full-cohort lineage differs from the historical evidence')
        print(json.dumps(report), flush=True)
        return report
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--benchmarks', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    verify(args.benchmarks, args.output)
