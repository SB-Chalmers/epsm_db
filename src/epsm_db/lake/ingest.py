"""Bounded COPY ingestion with a file checkpoint, replacing policy/work-unit DAGs."""
import glob
import hashlib
import json
from pathlib import Path
import tomllib

from psycopg.types.json import Jsonb

from .readers import normalize, rows
from .progress import report as message

KINDS = ('components', 'buildings', 'properties', 'addresses', 'heights', 'epc', 'areas', 'statistics', 'roads')
# Each statement is a set-based operation on one COPY batch. Table names and
# columns are application constants; source values remain bound parameters/JSON.
INCOMING = """WITH incoming AS (
 SELECT DISTINCT ON (payload->>'external_id') payload AS d,geom,record_no
 FROM ingest_batch ORDER BY payload->>'external_id',record_no DESC)
"""
WRITES = {
'components': [
 """INSERT INTO lake.building(external_id,municipality_code,municipality_conflict)
    SELECT payload->>'building_external_id',
       CASE WHEN count(DISTINCT payload->>'municipality_code')=1 THEN min(payload->>'municipality_code') END,
       count(DISTINCT payload->>'municipality_code')>1 OR bool_or((payload->>'municipality_conflict')::boolean)
    FROM ingest_batch GROUP BY payload->>'building_external_id'
    ON CONFLICT(external_id) DO UPDATE SET municipality_code=
       CASE WHEN lake.building.municipality_conflict OR EXCLUDED.municipality_conflict
         OR (lake.building.municipality_code IS NOT NULL AND EXCLUDED.municipality_code IS NOT NULL
             AND lake.building.municipality_code<>EXCLUDED.municipality_code) THEN NULL
         ELSE COALESCE(EXCLUDED.municipality_code,lake.building.municipality_code) END,
       municipality_conflict=lake.building.municipality_conflict OR EXCLUDED.municipality_conflict
         OR (lake.building.municipality_code IS NOT NULL AND EXCLUDED.municipality_code IS NOT NULL
             AND lake.building.municipality_code<>EXCLUDED.municipality_code)""",
 INCOMING + """INSERT INTO lake.component(external_id,building_id,geom,geometry_valid,file_id,source_record,attributes)
    SELECT d->>'external_id',b.building_id,i.geom::geometry(MultiPolygon,3006),
           (d->>'geometry_valid')::boolean,%(file_id)s,record_no::text,d->'attributes'
    FROM incoming i JOIN lake.building b ON b.external_id=d->>'building_external_id'
    ON CONFLICT(external_id) DO UPDATE SET geom=EXCLUDED.geom,geometry_valid=EXCLUDED.geometry_valid,
      attributes=EXCLUDED.attributes,source_record=EXCLUDED.source_record WHERE lake.component.file_id=EXCLUDED.file_id""",
 """UPDATE lake.building SET geometry_dirty=true WHERE external_id IN
    (SELECT payload->>'building_external_id' FROM ingest_batch)"""],
'buildings': [
 """WITH incoming AS (
 SELECT DISTINCT ON (payload->>'external_id') payload AS d,geom,record_no
 FROM ingest_batch ORDER BY payload->>'external_id',
 COALESCE((payload->>'object_version')::integer,0) DESC,
 COALESCE((payload->>'valid_from')::timestamptz,'-infinity'::timestamptz) DESC,record_no DESC)
""" + """INSERT INTO lake.building(external_id,municipality_code,house_number,construction_year,
       object_status,purposes,attributes,registry_file_id,registry_source_record,object_version,valid_from)
    SELECT d->>'external_id',d->>'municipality_code',(d->>'house_number')::integer,
       (d->>'construction_year')::integer,d->>'object_status',d->'purposes',d->'attributes',%(file_id)s,record_no::text,
       (d->>'object_version')::integer,(d->>'valid_from')::timestamptz FROM incoming
    ON CONFLICT(external_id) DO UPDATE SET municipality_code=CASE WHEN lake.building.municipality_conflict THEN NULL ELSE COALESCE(EXCLUDED.municipality_code,lake.building.municipality_code) END,
       house_number=EXCLUDED.house_number,construction_year=EXCLUDED.construction_year,
       object_status=EXCLUDED.object_status,purposes=EXCLUDED.purposes,attributes=EXCLUDED.attributes,
       registry_file_id=EXCLUDED.registry_file_id,registry_source_record=EXCLUDED.registry_source_record,object_version=EXCLUDED.object_version,valid_from=EXCLUDED.valid_from
    WHERE ROW(COALESCE(EXCLUDED.object_version,0),COALESCE(EXCLUDED.valid_from,'-infinity'::timestamptz)) >=
          ROW(COALESCE(lake.building.object_version,0),COALESCE(lake.building.valid_from,'-infinity'::timestamptz))""",
 """DELETE FROM lake.building_address l USING lake.building b,ingest_batch i
    WHERE l.building_id=b.building_id AND l.method='source_reference'
      AND b.external_id=i.payload->>'external_id'
      AND b.registry_file_id=%(file_id)s AND b.attributes=i.payload->'attributes'
      AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(i.payload->'address_external_ids') a
                     WHERE a.value=l.address_external_id)""",
 """INSERT INTO lake.building_address(building_id,address_external_id)
    SELECT DISTINCT b.building_id,a.value FROM ingest_batch i
    JOIN lake.building b ON b.external_id=i.payload->>'external_id'
    CROSS JOIN LATERAL jsonb_array_elements_text(i.payload->'address_external_ids') a
    WHERE b.registry_file_id=%(file_id)s AND b.attributes=i.payload->'attributes'
    ON CONFLICT(building_id,address_external_id) DO NOTHING"""],
'properties': [
 INCOMING + """INSERT INTO lake.property(external_id,municipality_code,designation,designation_key)
    SELECT d->>'external_id',d->>'municipality_code',d->>'designation',d->>'designation_key' FROM incoming WHERE d->>'external_id' IS NOT NULL
    ON CONFLICT(external_id) DO UPDATE SET municipality_code=COALESCE(EXCLUDED.municipality_code,lake.property.municipality_code),
       designation=COALESCE(EXCLUDED.designation,lake.property.designation),
       designation_key=COALESCE(EXCLUDED.designation_key,lake.property.designation_key),geometry_dirty=true""",
 """WITH incoming AS (SELECT DISTINCT ON (payload->>'part_external_id') payload AS d,geom,record_no
    FROM ingest_batch ORDER BY payload->>'part_external_id',record_no DESC)
    INSERT INTO lake.property_part(external_id,property_id,municipality_code,designation,designation_key,geom,file_id,source_record,attributes)
    SELECT d->>'part_external_id',p.property_id,d->>'municipality_code',d->>'designation',d->>'designation_key',
       i.geom::geometry(MultiPolygon,3006),%(file_id)s,record_no::text,d->'attributes'
    FROM incoming i LEFT JOIN lake.property p ON p.external_id=d->>'external_id'
    ON CONFLICT(external_id) DO UPDATE SET geom=EXCLUDED.geom,attributes=EXCLUDED.attributes,source_record=EXCLUDED.source_record,
       municipality_code=EXCLUDED.municipality_code,designation=EXCLUDED.designation,designation_key=EXCLUDED.designation_key
    WHERE lake.property_part.file_id=EXCLUDED.file_id"""],
'addresses': [
 """WITH incoming AS (
 SELECT DISTINCT ON (payload->>'external_id') payload AS d,geom,record_no
 FROM ingest_batch ORDER BY payload->>'external_id',
 COALESCE((payload->>'object_version')::integer,0) DESC,
 COALESCE((payload->>'valid_from')::timestamptz,'-infinity'::timestamptz) DESC,record_no DESC)
""" + """INSERT INTO lake.address(external_id,municipality_code,street_name,address_number,locality,
       geom,file_id,source_record,attributes,object_version,valid_from)
    SELECT d->>'external_id',d->>'municipality_code',d->>'street_name',d->>'address_number',d->>'locality',
       geom::geometry(Point,3006),%(file_id)s,record_no::text,d->'attributes',
       (d->>'object_version')::integer,(d->>'valid_from')::timestamptz FROM incoming
    ON CONFLICT(external_id) DO UPDATE SET municipality_code=EXCLUDED.municipality_code,
       street_name=EXCLUDED.street_name,address_number=EXCLUDED.address_number,locality=EXCLUDED.locality,
       geom=EXCLUDED.geom,attributes=EXCLUDED.attributes,file_id=EXCLUDED.file_id,source_record=EXCLUDED.source_record,
       object_version=EXCLUDED.object_version,valid_from=EXCLUDED.valid_from
    WHERE ROW(COALESCE(EXCLUDED.object_version,0),COALESCE(EXCLUDED.valid_from,'-infinity'::timestamptz)) >=
          ROW(COALESCE(lake.address.object_version,0),COALESCE(lake.address.valid_from,'-infinity'::timestamptz))"""],
'heights': [
 """INSERT INTO lake.height(component_external_id,height_status,representative_height_m,eave_height_m,
       ridge_height_m,roof_form,measured_at,evidence_path,file_id,source_record,attributes)
    SELECT payload->>'component_external_id',payload->>'height_status',
       (payload->>'representative_height_m')::double precision,(payload->>'eave_height_m')::double precision,
       (payload->>'ridge_height_m')::double precision,payload->>'roof_form',(payload->>'measured_at')::timestamptz,
       payload->>'evidence_path',%(file_id)s,record_no::text,payload->'attributes' FROM ingest_batch
    ON CONFLICT(file_id,source_record) DO NOTHING"""],
'epc': [
 """WITH incoming AS (
    SELECT payload->>'external_id' AS external_id,
       (array_agg(payload->>'regime' ORDER BY record_no DESC))[1] AS regime,
       (array_agg((payload->>'certificate_date')::date ORDER BY record_no DESC)
          FILTER(WHERE payload->>'certificate_date' IS NOT NULL))[1] AS certificate_date,
       (array_agg(payload->>'energy_class' ORDER BY record_no DESC)
          FILTER(WHERE payload->>'energy_class' IS NOT NULL))[1] AS energy_class,
       (array_agg((payload->>'atemp_m2')::double precision ORDER BY record_no DESC)
          FILTER(WHERE payload->>'atemp_m2' IS NOT NULL))[1] AS atemp_m2
    FROM ingest_batch GROUP BY payload->>'external_id')
    INSERT INTO lake.epc(external_id,regime,certificate_date,energy_class,atemp_m2)
    SELECT external_id,regime,certificate_date,energy_class,atemp_m2 FROM incoming
    ON CONFLICT(external_id) DO UPDATE SET
       certificate_date=COALESCE(EXCLUDED.certificate_date,lake.epc.certificate_date),
       energy_class=COALESCE(EXCLUDED.energy_class,lake.epc.energy_class),
       atemp_m2=COALESCE(EXCLUDED.atemp_m2,lake.epc.atemp_m2)""",
 """INSERT INTO lake.epc_record(epc_id,file_id,source_record,attributes)
    SELECT e.epc_id,%(file_id)s,record_no::text,payload->'attributes' FROM ingest_batch i
    JOIN lake.epc e ON e.external_id=i.payload->>'external_id' ON CONFLICT(file_id,source_record) DO NOTHING""",
 """INSERT INTO lake.epc_subject(epc_id,municipality_code,designation_key,house_number,building_external_id)
    SELECT DISTINCT e.epc_id,payload->>'municipality_code',payload->>'designation_key',
       (payload->>'house_number')::integer,payload->>'building_external_id'
    FROM ingest_batch i JOIN lake.epc e ON e.external_id=i.payload->>'external_id'
    ON CONFLICT DO NOTHING"""],
}


STATISTICS_WRITE = """INSERT INTO lake.statistic(area_type,area_external_id,boundary_version,
 indicator,period,unit,value,raw_value,value_status,dimensions,source_field,file_id,source_record,attributes)
 SELECT o.v->>'area_type',o.v->>'area_external_id',o.v->>'boundary_version',
 o.v->>'indicator',o.v->>'period',o.v->>'unit',(o.v->>'value')::double precision,
 o.v->>'raw_value',o.v->>'value_status',o.v->'dimensions',o.v->>'source_field',
 %(file_id)s,i.record_no::text,o.v->'attributes'
 FROM ingest_batch i CROSS JOIN LATERAL jsonb_array_elements(i.payload->'observations') o(v)
 ON CONFLICT(file_id,source_record,source_field,indicator,period,dimensions) DO NOTHING"""
WRITES['areas'] = ["""WITH incoming AS (
 SELECT DISTINCT ON (payload->>'area_type',payload->>'area_external_id',payload->>'boundary_version')
 payload AS d,geom,record_no FROM ingest_batch
 ORDER BY payload->>'area_type',payload->>'area_external_id',payload->>'boundary_version',record_no DESC)
 INSERT INTO lake.area(area_type,external_id,boundary_version,name,municipality_code,
 parent_type,parent_external_id,parent_boundary_version,geom,geometry_valid,file_id,source_record,attributes)
 SELECT d->>'area_type',d->>'area_external_id',d->>'boundary_version',d->>'name',d->>'municipality_code',
 d->>'parent_type',d->>'parent_external_id',d->>'parent_boundary_version',geom::geometry(MultiPolygon,3006),
 (d->>'geometry_valid')::boolean,%(file_id)s,record_no::text,d->'attributes' FROM incoming
 ON CONFLICT(area_type,external_id,boundary_version) DO UPDATE SET geom=EXCLUDED.geom,
 geometry_valid=EXCLUDED.geometry_valid,name=EXCLUDED.name,attributes=EXCLUDED.attributes,
 municipality_code=EXCLUDED.municipality_code,parent_type=EXCLUDED.parent_type,
 parent_external_id=EXCLUDED.parent_external_id,parent_boundary_version=EXCLUDED.parent_boundary_version,
 source_record=EXCLUDED.source_record WHERE lake.area.file_id=EXCLUDED.file_id""", STATISTICS_WRITE]
WRITES['statistics'] = [STATISTICS_WRITE]
WRITES['roads'] = [INCOMING + """INSERT INTO lake.road(external_id,source_layer,source_member,
 network_link_id,start_measure,end_measure,geom,geometry_valid,file_id,source_record,attributes)
 SELECT d->>'external_id',d->>'source_layer',d->>'source_member',d->>'network_link_id',
 (d->>'start_measure')::double precision,(d->>'end_measure')::double precision,geom,
 (d->>'geometry_valid')::boolean,%(file_id)s,record_no::text,d->'attributes' FROM incoming
 ON CONFLICT(external_id) DO UPDATE SET geom=EXCLUDED.geom,geometry_valid=EXCLUDED.geometry_valid,
 attributes=EXCLUDED.attributes,source_record=EXCLUDED.source_record WHERE lake.road.file_id=EXCLUDED.file_id"""]


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path, *, source_root=None, kinds=None):
    path = Path(path).resolve()
    manifest = tomllib.loads(path.read_text())
    if manifest.get('schema_version') != 1:
        raise ValueError('Expected GIS lake manifest schema_version=1')
    root = Path(source_root or manifest.get('root', '.')).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() if source_root else path.parent) / root
    entries = manifest.get('sources', [])
    if not isinstance(entries, list) or not entries:
        raise ValueError('Manifest must declare at least one [[sources]] entry')
    names = set()
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get('kind') not in KINDS
            or not all(isinstance(entry.get(key), str) and entry[key] for key in ('name', 'version', 'path'))):
            raise ValueError('Every source needs a unique name, version and supported kind')
        if entry['name'] in names:
            raise ValueError('Source names must be unique within the manifest')
        names.add(entry['name'])
        if entry['kind'] in ('components', 'properties', 'addresses', 'areas', 'roads') and entry.get('srid', manifest.get('srid')) != 3006:
            raise ValueError('Spatial sources must explicitly declare srid=3006')
        if kinds and entry['kind'] not in kinds:
            continue
        paths = sorted(Path(p).resolve() for p in glob.glob(str(root / entry['path']), recursive=True) if Path(p).is_file())
        if not paths:
            raise FileNotFoundError(f"No source files match {entry['name']}: {entry['path']}")
        entry['_files'] = paths
    return root.resolve(), [entry for entry in entries if '_files' in entry]


def batch_rows(values, max_rows, max_bytes):
    batch, size = [], 0
    for value in values:
        estimate = len(json.dumps(value[1], ensure_ascii=False).encode()) + len(value[2] or '')
        if estimate > max_bytes:
            raise ValueError('One source record exceeds --batch-bytes')
        if batch and (len(batch) >= max_rows or size + estimate > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append(value)
        size += estimate
    if batch:
        yield batch


def ingest_manifest(lake, manifest, *, source_root=None, kinds=None, batch_size=4096, batch_bytes=32 * 1024**2,
                    file_checksums=False):
    if batch_size < 1 or batch_bytes < 1:
        raise ValueError('Batch limits must be positive')
    if kinds and set(kinds) - set(KINDS):
        raise ValueError('Unsupported source kind')
    root, entries = load_manifest(manifest, source_root=source_root, kinds=kinds)
    completed, skipped = 0, 0
    with lake.writer() as connection:
        connection.execute('''CREATE TEMP TABLE ingest_batch(record_no bigint,payload jsonb,
                              geom geometry(Geometry,3006)) ON COMMIT DELETE ROWS''')
        for entry in entries:
            if kinds and entry['kind'] not in kinds:
                continue
            options = {k: v for k, v in entry.items() if k not in ('_files', 'path', 'name', 'version', 'kind')}
            with connection.transaction():
                source = connection.execute('SELECT source_id,kind,version,options FROM lake.source WHERE name=%s', (entry['name'],)).fetchone()
                if source:
                    if (source[1], source[2], source[3]) != (entry['kind'], entry['version'], options):
                        raise ValueError('Source version/options changed. Create a fresh snapshot database for a new release.')
                    source_id = source[0]
                else:
                    source_id = connection.execute('INSERT INTO lake.source(name,kind,version,options) VALUES(%s,%s,%s,%s) RETURNING source_id',
                                                   (entry['name'], entry['kind'], entry['version'], Jsonb(options))).fetchone()[0]
            for path in entry['_files']:
                try:
                    relative = str(path.relative_to(root))
                except ValueError:
                    raise ValueError('Source files must be inside the declared manifest root') from None
                stat = path.stat()
                fingerprint = (stat.st_size, stat.st_mtime_ns)
                # Optional once-per-file verification, never hashes per geometry/row.
                digest = checksum(path) if file_checksums else None
                checked_stat = path.stat()
                if (checked_stat.st_size, checked_stat.st_mtime_ns) != fingerprint:
                    raise ValueError(f'Source changed while checking it: {relative}')
                with connection.transaction():
                    old = connection.execute('SELECT file_id,byte_size,mtime_ns,state,committed_rows,checksum FROM lake.import_file WHERE source_id=%s AND path=%s', (source_id, relative)).fetchone()
                    if old:
                        if (old[1], old[2]) != fingerprint or old[5] and digest and old[5] != digest:
                            raise ValueError(f'Source file changed after import/checkpoint: {relative}')
                        if old[3] == 'complete':
                            skipped += 1
                            message('file_skipped', source=entry['name'], path=relative)
                            continue
                        file_id, committed = old[0], old[4]
                    else:
                        file_id = connection.execute('INSERT INTO lake.import_file(source_id,path,byte_size,mtime_ns,checksum) VALUES(%s,%s,%s,%s,%s) RETURNING file_id', (source_id, relative, *fingerprint, digest)).fetchone()[0]
                        committed = 0
                    connection.execute("UPDATE lake.import_file SET state='loading',error=NULL,updated_at=now() WHERE file_id=%s", (file_id,))
                try:
                    message('file_started', source=entry['name'], path=relative, resume_row=committed)
                    def transformed():
                        for ordinal, row, geometry in rows(path, entry, skip=committed):
                            # Opaque upstream component IDs may contain legacy hashes;
                            # no hash is calculated, verified, or used as an internal key.
                            fid = row.get('_fid')
                            record = f"{entry['name']}/{relative}:{row.get('_source_member','')}:{row.get('_source_layer','')}:{ordinal if fid is None else fid}"
                            try:
                                data, wkb = normalize(entry['kind'], row, geometry, entry, record)
                            except (ValueError, TypeError, KeyError) as error:
                                raise ValueError(f'{relative}: record {ordinal}: {error}') from error
                            yield ordinal, data, wkb
                    for batch in batch_rows(transformed(), batch_size, batch_bytes):
                        with connection.transaction():
                            with connection.cursor().copy('COPY ingest_batch(record_no,payload,geom) FROM STDIN') as copy:
                                for ordinal, data, wkb in batch:
                                    copy.write_row((ordinal, Jsonb(data), wkb))
                            connection.execute('ANALYZE ingest_batch')
                            collision_table = {'components': 'component', 'properties': 'property_part'}.get(entry['kind'])
                            if collision_table:
                                key = 'part_external_id' if entry['kind'] == 'properties' else 'external_id'
                                # Static names selected only from the constants above.
                                collision = connection.execute(f"SELECT 1 FROM lake.{collision_table} t JOIN ingest_batch s ON t.external_id=s.payload->>'{key}' WHERE t.file_id<>%s LIMIT 1", (file_id,)).fetchone()
                                if collision:
                                    raise ValueError('A source identifier occurs in multiple files/releases; resolve the source overlap explicitly.')
                                owner_table, owner_id, owner_field = (
                                    ('building', 'building_id', 'building_external_id') if entry['kind']=='components'
                                    else ('property', 'property_id', 'external_id'))
                                mixed = connection.execute(f"SELECT 1 FROM ingest_batch GROUP BY payload->>'{key}' HAVING count(DISTINCT COALESCE(payload->>'{owner_field}','<missing-source-reference>'))>1 LIMIT 1").fetchone()
                                reassigned = connection.execute(f"SELECT 1 FROM lake.{collision_table} t LEFT JOIN lake.{owner_table} p ON p.{owner_id}=t.{owner_id} JOIN ingest_batch s ON t.external_id=s.payload->>'{key}' WHERE p.external_id IS DISTINCT FROM s.payload->>'{owner_field}' LIMIT 1").fetchone()
                                if mixed or reassigned:
                                    raise ValueError('A component/part identifier names different owning entities; resolve the source identity explicitly.')
                            if entry['kind']=='areas':
                                collision = connection.execute('''SELECT 1 FROM lake.area a JOIN ingest_batch s
                                    ON a.area_type=s.payload->>'area_type' AND a.external_id=s.payload->>'area_external_id'
                                    AND a.boundary_version=s.payload->>'boundary_version' WHERE a.file_id<>%s LIMIT 1''', (file_id,)).fetchone()
                                if collision:
                                    raise ValueError('A boundary code/edition occurs in different source files; choose its authoritative source explicitly.')
                            if entry['kind']=='roads':
                                collision = connection.execute('''SELECT 1 FROM lake.road r JOIN ingest_batch s
                                    ON r.external_id=s.payload->>'external_id' WHERE r.file_id<>%s LIMIT 1''', (file_id,)).fetchone()
                                if collision:
                                    raise ValueError('A road feature identity occurs in different source files; use distinct source feature IDs.')
                            for statement in WRITES[entry['kind']]:
                                connection.execute(statement, {'file_id': file_id} if '%(file_id)s' in statement else None)
                            committed = batch[-1][0]
                            connection.execute('UPDATE lake.import_file SET committed_rows=%s,updated_at=clock_timestamp() WHERE file_id=%s', (committed, file_id))
                            connection.execute('''UPDATE lake.publication SET revision=revision+1,
                                property_links_dirty=property_links_dirty OR %s,
                                epc_links_dirty=epc_links_dirty OR %s,
                                area_links_dirty=area_links_dirty OR %s''',
                                (entry['kind'] in ('components', 'properties'),
                                 entry['kind'] in ('components', 'properties', 'buildings', 'epc'),
                                 entry['kind'] in ('components', 'areas')))
                        message('batch_committed', source=entry['name'], path=relative, committed_rows=committed)
                    after = path.stat()
                    if (after.st_size, after.st_mtime_ns) != fingerprint:
                        raise ValueError('Source file changed during ingestion; discard this snapshot database and reimport stable sources.')
                    connection.execute("UPDATE lake.import_file SET state='complete',updated_at=now() WHERE file_id=%s", (file_id,))
                    completed += 1
                    message('file_complete', source=entry['name'], path=relative, committed_rows=committed)
                except Exception as error:
                    connection.execute("UPDATE lake.import_file SET state='failed',error=%s,updated_at=now() WHERE file_id=%s", (str(error)[:2000], file_id))
                    raise
    return {'completed_files': completed, 'skipped_files': skipped, 'next': 'epsm-db link; epsm-db publish'}
