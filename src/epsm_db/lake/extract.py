"""Stream a consistent, filtered GIS snapshot to a portable GeoPackage."""
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from psycopg import sql

from .database import LAYERS
from .scb import SCB_LAYERS
from .progress import report

ALIASES = {'components', 'footprints', 'property_parts', 'deso', 'regso', 'national_boundary',
           'counties', 'municipalities', 'population_grids', 'area_statistics'} | set(SCB_LAYERS)
DEFAULT_LAYERS = ('buildings', 'component_heights', 'properties', 'addresses',
                  'epc_links', 'roads', 'boundaries')


def _string(value):
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def extract(lake, output, *, layers=DEFAULT_LAYERS, municipality_code=None,
            bbox=None, area_type=None, area_code=None, boundary_version=None,
            batch_rows=10000, _stock=None):
    """Export whole intersecting features, never clipped or silently row-limited.

    Municipality filters use source municipality codes on entity layers and an
    explicit boundary on road/context layers. Area filters use ST_Intersects.
    All coordinates and the GeoPackage use EPSG:3006. JSON attributes become text.
    Existing output files are never overwritten; partial output is temporary.
    """
    import geopandas as gpd
    import pandas as pd
    import pyogrio
    import shapely

    output = Path(output).expanduser().resolve()
    layers = tuple(dict.fromkeys(layers))
    if not layers or set(layers) - (set(LAYERS) | ALIASES):
        raise ValueError('Choose published GIS map layers')
    if output.suffix.lower() != '.gpkg':
        raise ValueError('Extract output must end in .gpkg')
    if output.exists():
        raise FileExistsError(f'Output already exists: {output}')
    if not isinstance(batch_rows, int) or not 1 <= batch_rows <= 100000:
        raise ValueError('batch_rows must be between 1 and 100000')
    if municipality_code is not None:
        municipality_code = str(municipality_code).zfill(4)
        if len(municipality_code) != 4 or not municipality_code.isdigit():
            raise ValueError('municipality_code must be a four-digit code')
    if (area_type is None) != (area_code is None):
        raise ValueError('Supply both area_type and area_code')
    if boundary_version is not None and area_type is None and municipality_code is None:
        raise ValueError('boundary_version needs an area or municipality selection')
    if bbox is not None:
        bbox = tuple(float(v) for v in bbox)
        if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox) or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError('bbox must be (xmin,ymin,xmax,ymax) in EPSG:3006')
    if bbox is None and municipality_code is None and area_type is None and _stock is None:
        raise ValueError('Choose a municipality, area or bbox for an extract')
    metadata_path = output.with_suffix('.metadata.json')
    if metadata_path.exists():
        raise FileExistsError(f'Metadata already exists: {metadata_path}')
    output.parent.mkdir(parents=True, exist_ok=True)
    counts = {}
    geometry_types = {'addresses': 'Point', 'roads': 'MultiLineString', 'road_points': 'MultiPoint'}
    with TemporaryDirectory(prefix='.epsm-extract-', dir=output.parent) as temporary:
        prepared = Path(temporary) / output.name
        with lake.connect() as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            publication_cursor = connection.execute('SELECT * FROM gis.publication_status')
            publication = dict(zip((column.name for column in publication_cursor.description), publication_cursor.fetchone()))

            if _stock is not None:
                from psycopg.rows import dict_row
                # This exporter uses tuple rows; check the fence through a dict cursor.
                with connection.cursor(row_factory=dict_row) as check:
                    checked = check.execute('SELECT current_database() AS dataset_id,* FROM gis.publication_status').fetchone()
                from ..errors import PublicationPendingError
                if 'model_revision' not in checked or any(checked[k] != checked['source_revision']
                        for k in ('linked_revision','published_revision','model_revision')):
                    raise PublicationPendingError('Finish ingestion, linking and model publication before extracting a stock.')
                # Resolve selection parameters with dict rows, restoring the exporter factory afterwards.
                previous_factory = connection.row_factory
                connection.row_factory = dict_row
                try:
                    stock_query, stock_values = _stock._selection_sql(connection)
                finally:
                    connection.row_factory = previous_factory
            resolved_boundaries = {}

            def boundary(kind, code):
                query = 'SELECT fid,boundary_version FROM gis.boundaries WHERE area_type=%s AND area_code=%s'
                values = [kind, str(code)]
                if boundary_version is not None:
                    query += ' AND boundary_version=%s'
                    values.append(str(boundary_version))
                found = connection.execute(query, values).fetchall()
                if len(found) != 1:
                    raise ValueError(f'Expected one published boundary for {kind}/{code}; found {len(found)}. Specify its boundary_version or import its boundary first.')
                resolved_boundaries[str(found[0][0])] = {'area_type': kind, 'area_code': str(code), 'boundary_version': found[0][1]}
                return found[0][0]

            selected_area = boundary(area_type, area_code) if area_type else None
            for layer in layers:
                relation = 'model_export_buildings' if _stock is not None and layer in {'buildings','footprints'} else layer
                columns = [r[0] for r in connection.execute(
                    'SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum',
                    ('gis.' + relation,)) if r[0] != 'geom']
                selected = sql.SQL(',').join(sql.SQL('f.{}').format(sql.Identifier(c)) for c in columns)
                query = sql.SQL('SELECT {},ST_AsBinary(f.geom) AS geometry FROM gis.{} f').format(selected, sql.Identifier(relation))
                filters, values = [], []
                boundary_ids = []
                if municipality_code is not None:
                    if 'municipality_code' in columns and layer not in {'boundaries','deso','regso','national_boundary','counties','municipalities','population_grids'}:
                        filters.append(sql.SQL('f.municipality_code=%s'))
                        values.append(municipality_code)
                    else:
                        boundary_ids.append(boundary('municipality', municipality_code))
                if selected_area is not None:
                    boundary_ids.append(selected_area)
                for boundary_id in boundary_ids:
                    filters.append(sql.SQL('EXISTS (SELECT 1 FROM gis.boundaries a WHERE a.fid=%s AND f.geom && a.geom AND ST_Intersects(f.geom,a.geom))'))
                    values.append(boundary_id)
                if bbox is not None:
                    filters.append(sql.SQL('f.geom && ST_MakeEnvelope(%s,%s,%s,%s,3006) AND ST_Intersects(f.geom,ST_MakeEnvelope(%s,%s,%s,%s,3006))'))
                    values.extend(bbox)
                    values.extend(bbox)
                if _stock is not None:
                    building_column = {'buildings': 'fid', 'footprints': 'fid', 'component_heights': 'building_id',
                        'components': 'building_id', 'epc_links': 'building_id', 'building_properties': 'building_id'}.get(layer)
                    if building_column or layer in {'addresses','properties','property_parts'}:
                        # Related entities are complete even when they cross the geographic selection.
                        filters, values = [], []
                    if building_column:
                        filters.append(sql.SQL('f.{} IN (SELECT fid FROM epsm_selected)').format(sql.Identifier(building_column)))
                    elif layer == 'addresses':
                        filters.append(sql.SQL('EXISTS (SELECT 1 FROM gis.building_addresses l JOIN epsm_selected s ON s.fid=l.building_id WHERE l.address_id=f.fid)'))
                    elif layer == 'properties':
                        filters.append(sql.SQL('EXISTS (SELECT 1 FROM gis.model_property_links l JOIN epsm_selected s ON s.fid=l.building_id WHERE l.property_id=f.fid)'))
                    elif layer == 'property_parts':
                        filters.append(sql.SQL('EXISTS (SELECT 1 FROM gis.model_property_links l JOIN epsm_selected s ON s.fid=l.building_id WHERE l.property_id=f.property_id) OR (f.property_id IS NULL AND EXISTS (SELECT 1 FROM gis.model_buildings b JOIN epsm_selected s ON s.fid=b.building_id WHERE b.geom && f.geom AND ST_Intersects(b.geom,f.geom)))'))
                    elif not any((municipality_code, bbox, area_type)):
                        raise ValueError('Choose a municipality, area or bbox to include context layers in a stock extract')
                    query = sql.SQL('WITH epsm_selected AS MATERIALIZED ({}) ').format(stock_query) + query
                    values = [*stock_values, *values]
                query += sql.SQL(' WHERE ') + (sql.SQL(' AND ').join(sql.SQL('({})').format(f) for f in filters) if filters else sql.SQL('true'))
                with connection.cursor(name='epsm_extract') as cursor:
                    cursor.execute(query, values)
                    description = cursor.description
                    total, first = 0, True
                    while True:
                        rows = cursor.fetchmany(batch_rows)
                        if not rows and not first:
                            break
                        frame = pd.DataFrame.from_records(rows, columns=columns + ['geometry'])
                        geometries = shapely.from_wkb([bytes(v) if v is not None else None for v in frame.pop('geometry')])
                        for index, column in enumerate(columns):
                            oid = description[index].type_code
                            if oid in (20, 21, 23):
                                frame[column] = pd.array(frame[column], dtype='Int64')
                            elif oid in (700, 701, 1700):
                                frame[column] = pd.array(frame[column], dtype='Float64')
                            elif oid == 16:
                                frame[column] = pd.array(frame[column], dtype='boolean')
                            else:
                                frame[column] = pd.array([_string(v) for v in frame[column]], dtype='string')
                        spatial = gpd.GeoDataFrame(frame, geometry=geometries, crs='EPSG:3006')
                        pyogrio.write_dataframe(spatial, prepared, layer=layer, driver='GPKG',
                            append=not first, geometry_type=geometry_types.get(layer, 'MultiPolygon'),
                            promote_to_multi=layer != 'addresses',
                            layer_options={'FID': 'gpkg_fid', 'GEOMETRY_NAME': 'geom', 'SPATIAL_INDEX': 'YES'} if first else None)
                        total += len(rows)
                        first = False
                        if not rows:
                            break
                    counts[layer] = total
                    report('extract_layer_complete', layer=layer, features=total)
            metadata = {'database': connection.info.dbname, 'publication': publication,
                        'selection': {'municipality_code': municipality_code, 'bbox': bbox,
                                      'area_type': area_type, 'area_code': area_code,
                                      'boundary_version': boundary_version,
                                      'stock': _stock.describe() if _stock is not None else None},
                        'layers': counts, 'crs': 'EPSG:3006', 'resolved_boundaries': resolved_boundaries,
                        'geometry': 'Whole selected features; not clipped. JSON attributes are text.',
                        'feature_ids': 'fid retains the source key; gpkg_fid is the GeoPackage primary key.'}
        # link() is atomic, refuses a concurrent pre-existing destination and
        # works on the same filesystem as the temporary output directory.
        os.link(prepared, output)
        with metadata_path.open('x') as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2, default=str)
            stream.write('\n')
    return {'output': str(output), 'layers': counts, 'metadata': str(metadata_path)}
