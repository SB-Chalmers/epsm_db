"""Streaming adapters for LM, Boverket and height files; no geometry digests."""
import csv
import gzip
import json
import math
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID


def text(value):
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return None
    value = str(value).strip()
    return value or None


def identifier(value):
    value = text(value)
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return value


def integer(value):
    value = text(value)
    if value is None:
        return None
    try:
        number = Decimal(value.replace(' ', '').replace(',', '.'))
        if number.is_finite() and number == number.to_integral_value():
            return int(number)
    except (ValueError, InvalidOperation):
        pass
    raise ValueError(f'Expected integer, got {value!r}')


def number(value):
    value = text(value)
    if value is None:
        return None
    result = float(value.replace(' ', '').replace(',', '.'))
    if not math.isfinite(result):
        raise ValueError('Non-finite measurement')
    return result


def municipality(value):
    value = text(value)
    if value is None:
        return None
    if not value.isdigit() or len(value) > 4:
        raise ValueError(f'Invalid municipality code: {value!r}')
    return value.zfill(4)


def designation(value):
    value = text(value)
    return ' '.join(unicodedata.normalize('NFKC', value).upper().split()).split('>', 1)[0].strip() if value else None


def json_value(value):
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, 'item'):
        return json_value(value.item())
    return value


def rows(path, entry, skip=0):
    """Yield source ordinal, values, geometry. Skip Parquet row groups on resume."""
    adapter = entry.get('adapter', entry['kind'])
    if path.suffix == '.xml':
        from ..lm_xml import _selected_elements, parse_address_element, parse_building_element
        parser, member = ((parse_building_element, 'Byggnad') if adapter == 'buildings'
                          else (parse_address_element, 'Belagenhetsadress'))
        if adapter not in ('buildings', 'addresses'):
            raise ValueError('XML supports building and address registers')
        for ordinal, element in _selected_elements(path, member, None):
            if ordinal > skip:
                yield ordinal, parser(element, source_file=path.name, source_record=ordinal), None
        return
    if path.suffix == '.parquet':
        import pyarrow.parquet as pq
        file = pq.ParquetFile(path)
        metadata = file.schema_arrow.metadata or {}
        if b'geo' in metadata:
            from pyproj import CRS
            geo = json.loads(metadata[b'geo'])
            primary = geo.get('primary_column', 'geometry')
            declared = geo.get('columns', {}).get(primary, {}).get('crs', 'OGC:CRS84')
            if declared is not None and CRS.from_user_input(declared) != CRS.from_epsg(3006):
                raise ValueError(f'Expected EPSG:3006 GeoParquet: {path}')
        position = 0
        for group in range(file.num_row_groups):
            count = file.metadata.row_group(group).num_rows
            if position + count <= skip:
                position += count
                continue
            for batch in file.iter_batches(row_groups=[group], batch_size=4096):
                for row in batch.to_pylist():
                    position += 1
                    if position > skip:
                        geometry = row.pop(entry.get('geometry_column', 'geometry'), None)
                        yield position, row, geometry
        return
    if path.suffix.lower() in ('.gpkg', '.fgb', '.shp', '.geojson', '.zip'):
        from .vector_sources import vector_rows
        yield from vector_rows(path, entry, skip)
        return
    if path.suffix in ('.json', '.jsonl', '.ndjson', '.gz'):
        opening = gzip.open if path.suffix == '.gz' else open
        with opening(path, 'rt', encoding='utf-8') as stream:
            if path.name.endswith('.json'):
                value = json.load(stream)
                values = value if isinstance(value, list) else [value]
            else:
                values = (json.loads(line) for line in stream if line.strip())
            for position, row in enumerate(values, 1):
                if position > skip:
                    yield position, row, row.pop(entry.get('geometry_column', 'geometry'), None)
        return
    if path.suffix in ('.txt', '.csv'):
        from ..epc import detect_encoding
        encoding = entry.get('encoding') or detect_encoding(path)
        with path.open(encoding=encoding, newline='') as stream:
            reader = csv.DictReader(stream, delimiter=entry.get('delimiter', '|' if adapter == 'epc' else ','))
            for position, row in enumerate(reader, 1):
                if position > skip:
                    yield position, row, None
        return
    raise ValueError(f'Unsupported source format: {path}')


def normalize(kind, row, geometry, entry, source_record):
    """Only declared mappings; unknown source fields remain in attributes."""
    import shapely
    mappings = entry.get('columns', {})
    def field(name, *fallback):
        if name in mappings:
            return row.get(mappings[name])
        return next((row[k] for k in (name,) + fallback if row.get(k) is not None), None)
    attrs = json_value({k: v for k, v in row.items() if k not in ('geometry', 'geometry_hash', 'input_geometry_hash')})
    geom = None
    if geometry is not None and kind in ('components', 'properties', 'addresses', 'areas', 'roads'):
        if isinstance(geometry, dict):
            from shapely.geometry import shape
            geom = shape(geometry)
        elif isinstance(geometry, str):
            geom = shapely.from_wkt(geometry) if not geometry.startswith(('00', '01')) else shapely.from_wkb(geometry)
        else:
            geom = shapely.from_wkb(geometry)
    if kind in ('components', 'properties', 'areas'):
        if geom is None or geom.is_empty:
            raise ValueError('A polygon source record has no geometry')
        if geom.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError(f'Expected Polygon/MultiPolygon, got {geom.geom_type}')
        if shapely.get_srid(geom) not in (0, 3006):
            raise ValueError('Geometry SRID differs from EPSG:3006')
        geom = shapely.force_2d(geom)
        if geom.geom_type == 'Polygon':
            geom = shapely.MultiPolygon([geom])
    if geom is not None:
        if shapely.get_srid(geom) not in (0, 3006):
            raise ValueError('Geometry SRID differs from EPSG:3006')
        geom = shapely.force_2d(geom)
    if kind in ('areas', 'statistics', 'roads'):
        from .geography import normalize_context
        data = normalize_context(kind, row, geom, entry, field, attrs, source_record)
        return data, geom.wkb_hex if geom is not None else None
    data = {'attributes': attrs}
    if kind == 'components':
        building = identifier(field('building_external_id', 'building_id', 'objektidentitet'))
        external = identifier(field('component_external_id', 'component_id'))
        if external is None:
            # Source feature identity, not the shape, defines a component.
            external = source_record
        municipalities = field('municipality_values') or []
        municipality_code = municipality(field('municipality_code', 'kommunkod'))
        if municipality_code is None and len(municipalities) == 1:
            municipality_code = municipality(municipalities[0])
        data.update(external_id=external, building_external_id=building,
                    municipality_code=municipality_code, municipality_conflict=len(municipalities)>1,
                    geometry_valid=bool(shapely.is_valid(geom)))
    elif kind == 'buildings':
        addresses = row.get('address_external_ids') or [e.get('address_place_id') for e in row.get('entrances', [])]
        data.update(external_id=identifier(field('external_id', 'building_id', 'objektidentitet')),
                    municipality_code=municipality(field('municipality_code', 'kommunkod')),
                    house_number=integer(field('house_number', 'husnummer')),
                    construction_year=integer(field('construction_year', 'nybyggnadsar')),
                    object_status=text(field('object_status', 'objektstatus')),
                    object_version=integer(field('object_version', 'objektversion')),
                    valid_from=text(field('valid_from')),
                    purposes=json_value(row.get('purposes') or []),
                    address_external_ids=sorted({identifier(v) for v in addresses if text(v)}))
    elif kind == 'properties':
        label = text(field('designation')) or ' '.join(str(row.get(k) or '') for k in ('trakt', 'etikett')).strip()
        data.update(external_id=identifier(field('property_external_id', 'property_unit_id', 'registerenhetsreferens')),
                    part_external_id=identifier(field('part_external_id', 'property_area_part_id', 'objektidentitet')) or source_record,
                    municipality_code=municipality(field('municipality_code', 'kommunkod')),
                    designation=label or None, designation_key=designation(label))
    elif kind == 'addresses':
        if geom is None and row.get('easting') is not None:
            geom = shapely.Point(float(row['easting']), float(row['northing']))
        if geom is not None and geom.geom_type != 'Point':
            raise ValueError('Address geometry must be Point')
        data.update(external_id=identifier(field('external_id', 'address_place_id', 'objektidentitet')),
                    municipality_code=municipality(field('municipality_code', 'kommunkod')),
                    street_name=text(field('street_name')), address_number=text(field('address_number')),
                    locality=text(field('locality')), object_version=integer(field('object_version', 'objektversion')),
                    valid_from=text(field('valid_from')))
    elif kind == 'heights':
        data.update(component_external_id=identifier(field('component_external_id', 'component_id')),
                    height_status=text(field('height_status')) or 'not_determined',
                    representative_height_m=number(field('representative_height_m')),
                    eave_height_m=number(field('eave_height_m')), ridge_height_m=number(field('ridge_height_m')),
                    roof_form=text(field('roof_form')), measured_at=text(field('measured_at')),
                    evidence_path=text(field('evidence_path', 'evidence_record_path')))
    elif kind == 'epc':
        regime = entry.get('regime') or text(field('regime')) or 'source'
        certificate = text(field('certificate_external_id', 'certificate_id'))
        if certificate is None:
            form = integer(field('FormularId'))
            certificate = f'{regime}:{form}' if form is not None else None
        issues = []
        def optional(parser, value, name):
            try:
                return parser(value)
            except (ValueError, TypeError):
                issues.append(name)
                return None
        municipality_code = optional(municipality, field('municipality_code'), 'municipality_code')
        if municipality_code is None and row.get('IdLankod') and row.get('IdKommunkod'):
            county = optional(integer, row['IdLankod'], 'IdLankod')
            local = optional(integer, row['IdKommunkod'], 'IdKommunkod')
            if county is not None and local is not None and 0 < county < 100 and 0 <= local < 100:
                municipality_code = f'{county:02d}{local:02d}'
        approval = text(field('certificate_date', 'ExpertGodkannande'))
        if approval:
            approval = optional(lambda v: datetime.fromisoformat(v.replace('Z', '+00:00')).date().isoformat(), approval, 'certificate_date')
        # Additional energy indicators need an explicit columns mapping; raw values
        # remain in epc_record without guessing units or allocating certificate totals.
        data.update(external_id=certificate, regime=regime, municipality_code=municipality_code,
                    designation_key=designation(field('designation', 'IdFastBet')),
                    house_number=optional(integer, field('house_number', 'IdHusnr'), 'house_number'),
                    building_external_id=identifier(field('building_external_id')),
                    certificate_date=approval, energy_class=text(field('energy_class')),
                    atemp_m2=optional(number, field('atemp_m2', 'EgenAtemp'), 'atemp_m2'))
        if issues:
            data['attributes']['_normalization_issues'] = issues
    else:
        raise ValueError(f'Unknown source kind: {kind}')
    required = {'components': ('external_id', 'building_external_id'),
                'properties': ('part_external_id',),
                'heights': ('component_external_id',),
                'buildings': ('external_id',), 'addresses': ('external_id',), 'epc': ('external_id',)}[kind]
    if any(not data.get(key) for key in required):
        raise ValueError(f'Missing required source identifier: {required}')
    return data, geom.wkb_hex if geom is not None else None
