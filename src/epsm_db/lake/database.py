"""Small administration API; source imports and GIS publication are independent."""
from contextlib import contextmanager
import os
import math
from pathlib import Path
import sysconfig

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .progress import report
from .scb import SCB_LAYERS

SCHEMA_VERSION = 2
MODEL_LAYERS = ('model_epc_fields', 'model_certificates', 'model_facts')
LAYERS = ('component_heights', 'buildings', 'properties', 'addresses', 'building_properties', 'epc_links',
          'boundaries', 'roads', 'road_points', 'road_areas')
LOCK = 726013220461


def resource(name):
    checkout = Path(__file__).resolve().parents[3]
    local = checkout / 'sql/lake' / name
    if local.is_file():
        return local
    packaged = Path(sysconfig.get_path('data')) / 'share/epsm_db/sql/lake' / name
    if not packaged.is_file():
        raise FileNotFoundError(f'Missing lake SQL resource: {name}')
    return packaged


class Lake:
    def __init__(self, database_url=None):
        self.url = database_url or os.environ.get('EPSM_DATABASE_URL') or os.environ.get('UBEM_DATABASE_URL')
        if not self.url:
            raise ValueError('Set EPSM_DATABASE_URL or pass --database-url')

    def connect(self, **kwargs):
        kwargs.setdefault('application_name', 'epsm-db GIS lake')
        return psycopg.connect(self.url, **kwargs)

    @contextmanager
    def writer(self, initialized=True):
        # A session lock fences all writers while allowing commits between files/batches.
        # autocommit avoids idle transactions during file I/O.
        with self.connect(autocommit=True) as connection:
            if not connection.execute('SELECT pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0]:
                raise ValueError('Another lake import/link/publication is running. Retry after it finishes.')
            try:
                if initialized:
                    self.require_schema(connection)
                yield connection
            finally:
                connection.execute('SELECT pg_advisory_unlock(%s)', (LOCK,))

    @staticmethod
    def require_schema(connection):
        if not connection.execute("SELECT to_regclass('lake.schema_version')").fetchone()[0]:
            raise ValueError('This is not a GIS lake database. Use a fresh database and epsm-db init.')
        if connection.execute('SELECT version FROM lake.schema_version').fetchall() != [(SCHEMA_VERSION,)]:
            raise ValueError('Unsupported lake schema version; run epsm-db migrate for a version-1 lake.')
        if not all(connection.execute("SELECT to_regclass('lake.area'),to_regclass('lake.statistic'),to_regclass('lake.road')").fetchone()):
            raise ValueError('This database uses an earlier lake draft. Initialize a fresh database with this branch revision.')

    def initialize(self):
        with self.writer(initialized=False) as connection, connection.transaction():
            if connection.execute("SELECT to_regclass('lake.schema_version')").fetchone()[0]:
                self.require_schema(connection)
                return {'schema_version': SCHEMA_VERSION, 'created': False}
            if int(connection.info.server_version) < 150000:
                raise ValueError('The GIS lake requires PostgreSQL 15 or newer')
            occupied = connection.execute("""SELECT nspname FROM pg_namespace
                WHERE nspname IN ('lake','gis','catalog','stock','evidence','scope','mart')""").fetchall()
            if occupied:
                raise ValueError('Initialization requires a fresh database; existing application schemas were found.')
            connection.execute(resource('001_schema.sql').read_text(), prepare=False)
            connection.execute(resource('002_gis.sql').read_text(), prepare=False)
            connection.execute(resource('007_scb_views.sql').read_text(), prepare=False)
            connection.execute(resource('009_model_api.sql').read_text(), prepare=False)
            for layer in MODEL_LAYERS:
                connection.execute(sql.SQL('REFRESH MATERIALIZED VIEW gis.{}').format(sql.Identifier(layer)))
            connection.execute('UPDATE lake.publication SET linked_revision=revision,published_revision=revision,model_revision=revision,published_at=now()')
        return {'schema_version': SCHEMA_VERSION, 'created': True, 'gis_layers': list(LAYERS)}

    def migrate(self):
        """Extend a version-1 lake in place; publish before model record access."""
        with self.writer(initialized=False) as connection, connection.transaction():
            version = connection.execute('SELECT version FROM lake.schema_version').fetchone()[0]
            if version == SCHEMA_VERSION:
                return {'schema_version': version, 'changed': False}
            if version != 1:
                raise ValueError('Only a version-1 lake can migrate to version 2')
            connection.execute(resource('009_model_api.sql').read_text(), prepare=False)
            # Preserve existing reader principals without granting canonical-table access.
            roles = connection.execute("""SELECT DISTINCT grantee FROM information_schema.table_privileges
                WHERE table_schema='gis' AND privilege_type='SELECT' AND grantee<>current_user""").fetchall()
            for (role,) in roles:
                connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA gis TO {}').format(sql.Identifier(role)))
        return {'schema_version': SCHEMA_VERSION, 'changed': True, 'publication_required': True}

    def ingest(self, manifest, **kwargs):
        from .ingest import ingest_manifest
        return ingest_manifest(self, manifest, **kwargs)

    def link(self, minimum_overlap=0.5):
        if not 0.5 <= minimum_overlap <= 1:
            raise ValueError('minimum_overlap must be between 0.5 and 1')
        with self.writer() as connection, connection.transaction():
            dirty = connection.execute('SELECT property_links_dirty,epc_links_dirty,minimum_overlap,area_links_dirty FROM lake.publication').fetchone()
            report('prepare_geometry_and_references')
            connection.execute(resource('003_link.sql').read_text(), prepare=False)
            property_dirty = dirty[0] or dirty[2] != minimum_overlap
            if property_dirty:
                report('link_properties', minimum_overlap=minimum_overlap)
                # Only a range-checked numeric literal enters this owned script.
                statement = resource('004_property_links.sql').read_text().replace(
                    '%(minimum_overlap)s', str(float(minimum_overlap)))
                connection.execute(statement, prepare=False)
            if property_dirty or dirty[1]:
                report('link_epc')
                connection.execute(resource('005_epc_links.sql').read_text(), prepare=False)
            if dirty[3]:
                report('link_building_areas')
                connection.execute(resource('006_area_links.sql').read_text(), prepare=False)
            if dirty[2] != minimum_overlap:
                connection.execute('UPDATE lake.publication SET revision=revision+1')
            connection.execute('''UPDATE lake.publication SET linked_revision=revision,linked_at=clock_timestamp(),
                property_links_dirty=false,epc_links_dirty=false,area_links_dirty=false,minimum_overlap=%s''', (minimum_overlap,))
        return {'linked': True, 'minimum_overlap': minimum_overlap}

    def publish(self, *, force=False):
        with self.writer() as connection, connection.transaction():
            row = connection.execute('SELECT revision,linked_revision,published_revision FROM lake.publication').fetchone()
            if row[0] and row[0] != row[1]:
                raise ValueError('Run epsm-db link after importing sources, then publish.')
            # An API-only migration reuses the existing geometry publication.
            layers = LAYERS + MODEL_LAYERS if force or row[2] != row[0] else MODEL_LAYERS
            for layer in layers:
                report('publish_layer_started', layer=layer)
                populated = connection.execute("SELECT ispopulated FROM pg_matviews WHERE schemaname='gis' AND matviewname=%s", (layer,)).fetchone()[0]
                refresh = 'REFRESH MATERIALIZED VIEW CONCURRENTLY gis.{}' if populated else 'REFRESH MATERIALIZED VIEW gis.{}'
                connection.execute(sql.SQL(refresh).format(sql.Identifier(layer)))
                connection.execute(sql.SQL('ANALYZE gis.{}').format(sql.Identifier(layer)))
                report('publish_layer_prepared', layer=layer)
            connection.execute('UPDATE lake.publication SET published_revision=revision,model_revision=revision,published_at=clock_timestamp()')
        return {'published': list(layers), 'source_revision': row[0]}

    def grant_reader(self, role):
        with self.writer() as connection, connection.transaction():
            if not connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                raise ValueError('Create the participant database role before granting GIS access.')
            connection.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                sql.Identifier(connection.info.dbname), sql.Identifier(role)))
            connection.execute(sql.SQL('GRANT USAGE ON SCHEMA gis TO {}').format(sql.Identifier(role)))
            connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA gis TO {}').format(sql.Identifier(role)))
        return {'role': role, 'read_access': 'gis'}

    def features(self, layer='buildings', *, bbox=None, municipality_code=None, limit=1000,
                 area_type=None, boundary_version=None, indicator=None, period=None, source_layer=None):
        """Read a bounded GIS layer selection; bbox coordinates are EPSG:3006 metres."""
        allowed = set(LAYERS) | set(SCB_LAYERS) | {'components', 'footprints', 'property_parts', 'area_statistics', 'deso', 'regso',
                                  'national_boundary', 'counties', 'municipalities', 'population_grids'}
        if layer not in allowed or not isinstance(limit, int) or not 1 <= limit <= 100000:
            raise ValueError('Choose a GIS layer and a limit between 1 and 100000')
        with self.connect(autocommit=True) as connection:
            columns = [r[0] for r in connection.execute(
                'SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum',
                ('gis.' + layer,)) if r[0] != 'geom']
            if not columns:
                raise ValueError('GIS layers have not been initialized')
            selected = sql.SQL(',').join(sql.Identifier(c) for c in columns)
            query = sql.SQL('SELECT {},ST_AsBinary(geom) AS geometry FROM gis.{}').format(selected, sql.Identifier(layer))
            filters, values = [], []
            if bbox is not None:
                bbox = tuple(float(v) for v in bbox)
                if len(bbox) != 4 or not all(math.isfinite(float(v)) for v in bbox) or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                    raise ValueError('bbox must be finite (xmin,ymin,xmax,ymax) in EPSG:3006')
                filters.append(sql.SQL('geom && ST_MakeEnvelope(%s,%s,%s,%s,3006)'))
                values.extend(bbox)
            if municipality_code is not None:
                if 'municipality_code' not in columns:
                    raise ValueError('This layer has no municipality column; use a bbox or a relational query')
                filters.append(sql.SQL('municipality_code=%s'))
                values.append(str(municipality_code).zfill(4))
            for column, value in {'area_type': area_type, 'boundary_version': boundary_version,
                                  'indicator': indicator, 'period': period, 'source_layer': source_layer}.items():
                if value is not None:
                    if column not in columns:
                        raise ValueError(f'This layer has no {column} column')
                    filters.append(sql.SQL('{}=%s').format(sql.Identifier(column)))
                    values.append(str(value))
            if filters:
                query += sql.SQL(' WHERE ') + sql.SQL(' AND ').join(filters)
            query += sql.SQL(' ORDER BY fid LIMIT %s')
            values.append(limit)
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(query, values)
                return [dict(row, geometry=bytes(row['geometry']) if row['geometry'] is not None else None)
                        for row in cursor]

    def extract(self, output, **selection):
        """Stream a municipality, area or bbox selection into a GeoPackage."""
        from .extract import extract
        return extract(self, output, **selection)

    def status(self):
        with self.connect(autocommit=True) as connection:
            self.require_schema(connection)
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute('SELECT *,published_revision IS DISTINCT FROM revision AS refresh_needed FROM lake.publication')
                publication = cursor.fetchone()
                cursor.execute('''SELECT s.name,s.kind,f.state,count(*) AS files,
                    sum(f.committed_rows) AS committed_rows,max(f.updated_at) AS last_update
                    FROM lake.source s JOIN lake.import_file f USING(source_id)
                    GROUP BY s.name,s.kind,f.state ORDER BY s.name,f.state''')
                imports = cursor.fetchall()
                cursor.execute("SELECT s.name,f.path,f.error FROM lake.import_file f JOIN lake.source s USING(source_id) WHERE f.state='failed'")
                failures = cursor.fetchall()
                cursor.execute("SELECT matviewname,ispopulated FROM pg_matviews WHERE schemaname='gis' ORDER BY matviewname")
                layers = cursor.fetchall()
        return {'schema_version': SCHEMA_VERSION, 'publication': publication,
                'imports': imports, 'failures': failures, 'layers': layers}
