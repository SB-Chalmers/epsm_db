"""Re-read parsed building purposes after a parser fix, without a new snapshot.

A snapshot keeps one immutable version of each source, so ``ingest`` skips files it
has already imported. When only the *parser* changed (the source files did not),
this command reads the same building register files again and rewrites the two
values the parser derives from them, ``purposes`` and the stored ``attributes``,
for buildings whose stored record is otherwise identical. Nothing else is touched:
no building is created, no key, link, geometry or other domain changes.
"""
from pathlib import Path

from psycopg.types.json import Jsonb

from .ingest import batch_rows, checksum, load_manifest
from .progress import report as message
from .readers import normalize, rows

# The newest record version per building in a batch, as ``ingest`` chooses it.
INCOMING = """WITH incoming AS (
 SELECT DISTINCT ON (payload->>'external_id') payload AS d
 FROM reparse_batch ORDER BY payload->>'external_id',
 COALESCE((payload->>'object_version')::integer,0) DESC,
 COALESCE((payload->>'valid_from')::timestamptz,'-infinity'::timestamptz) DESC,record_no DESC)
"""
# The stored record must be the one this file wrote, and identical apart from the
# parsed purposes, so a building last written by another file or version is left alone.
SAME_RECORD = """b.external_id=i.d->>'external_id' AND b.registry_file_id=%(file_id)s
 AND (b.attributes - 'purposes') = ((i.d->'attributes') - 'purposes')"""
COUNT = INCOMING + """SELECT
 count(*) FILTER (WHERE b.purposes IS DISTINCT FROM i.d->'purposes'
                    OR b.attributes IS DISTINCT FROM i.d->'attributes'),
 count(*) FROM incoming i JOIN lake.building b ON """ + SAME_RECORD
UPDATE = INCOMING + """UPDATE lake.building b SET purposes=i.d->'purposes',attributes=i.d->'attributes'
 FROM incoming i WHERE """ + SAME_RECORD + """
 AND (b.purposes IS DISTINCT FROM i.d->'purposes' OR b.attributes IS DISTINCT FROM i.d->'attributes')"""


def reparse_purposes(lake, manifest, *, source_root=None, batch_size=4096, batch_bytes=32 * 1024**2,
                     dry_run=False, allow_mtime_change=False):
    """Rewrite ``purposes`` of already imported buildings from the same source files.

    Every building file of the manifest must already be completely imported from a
    source with the same name, version and options, and must be unchanged (size and
    modification time; with ``allow_mtime_change`` the size and, where one was
    recorded, the checksum). ``dry_run`` counts what would change and writes nothing.
    Run ``epsm-db link`` and ``epsm-db publish`` afterwards.
    """
    if batch_size < 1 or batch_bytes < 1:
        raise ValueError('Batch limits must be positive')
    root, entries = load_manifest(manifest, source_root=source_root, kinds=('buildings',))
    if not entries:
        raise ValueError('The manifest declares no buildings source')
    result = {'dry_run': dry_run, 'files': 0, 'records_read': 0, 'buildings_matched': 0, 'buildings_changed': 0}
    with lake.writer() as connection:
        connection.execute('CREATE TEMP TABLE reparse_batch(record_no bigint,payload jsonb) ON COMMIT DELETE ROWS')
        for entry in entries:
            options = {k: v for k, v in entry.items() if k not in ('_files', 'path', 'name', 'version', 'kind')}
            source = connection.execute('SELECT source_id,kind,version,options FROM lake.source WHERE name=%s',
                                        (entry['name'],)).fetchone()
            if not source or (source[1], source[2], source[3]) != (entry['kind'], entry['version'], options):
                raise ValueError(f"Source {entry['name']} is not imported with this version/options; "
                                 'reparse only re-reads an already imported source.')
            for path in entry['_files']:
                try:
                    relative = str(Path(path).relative_to(root))
                except ValueError:
                    raise ValueError('Source files must be inside the declared manifest root') from None
                old = connection.execute('''SELECT file_id,byte_size,mtime_ns,state,checksum FROM lake.import_file
                                            WHERE source_id=%s AND path=%s''', (source[0], relative)).fetchone()
                if not old or old[3] != 'complete':
                    raise ValueError(f'Not a completely imported file of this snapshot: {relative}')
                stat = path.stat()
                if stat.st_size != old[1]:
                    raise ValueError(f'Source file changed after import: {relative}')
                if stat.st_mtime_ns != old[2]:
                    if not allow_mtime_change:
                        raise ValueError(f'Source file modification time differs from the import: {relative}. '
                                         'Use --allow-mtime-change if the file was only copied.')
                    if old[4] and checksum(path) != old[4]:
                        raise ValueError(f'Source file checksum differs from the import: {relative}')
                file_id = old[0]
                message('reparse_file_started', source=entry['name'], path=relative)

                def transformed():
                    for ordinal, row, geometry in rows(path, entry):
                        record = f"{entry['name']}/{relative}:{row.get('_source_member','')}:{row.get('_source_layer','')}:{ordinal}"
                        try:
                            data, _ = normalize('buildings', row, geometry, entry, record)
                        except (ValueError, TypeError, KeyError) as error:
                            raise ValueError(f'{relative}: record {ordinal}: {error}') from error
                        yield ordinal, data, None

                for batch in batch_rows(transformed(), batch_size, batch_bytes):
                    with connection.transaction():
                        with connection.cursor().copy('COPY reparse_batch(record_no,payload) FROM STDIN') as copy:
                            for ordinal, data, _ in batch:
                                copy.write_row((ordinal, Jsonb(data)))
                        connection.execute('ANALYZE reparse_batch')
                        would_change, matched = connection.execute(COUNT, {'file_id': file_id}).fetchone()
                        changed = would_change
                        if not dry_run and would_change:
                            changed = connection.execute(UPDATE, {'file_id': file_id}).rowcount
                            # Purposes feed only the published building type; no link depends on them.
                            connection.execute('UPDATE lake.publication SET revision=revision+1')
                    result['records_read'] += len(batch)
                    result['buildings_matched'] += matched
                    result['buildings_changed'] += changed
                    message('reparse_batch', source=entry['name'], path=relative, last_record=batch[-1][0],
                            changed=changed)
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                    raise ValueError(f'Source file changed while it was read: {relative}')
                result['files'] += 1
                message('reparse_file_complete', source=entry['name'], path=relative)
    result['next'] = ('nothing written' if dry_run or not result['buildings_changed']
                      else 'epsm-db link; epsm-db publish')
    return result
