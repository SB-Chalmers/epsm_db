"""Normalize versioned statistical geography and observations without allocation."""
from .readers import text, number, municipality, json_value


def geographic_fields(row, entry, field):
    kind = text(field('area_type')) or text(entry.get('area_type'))
    edition = text(field('boundary_version')) or text(entry.get('boundary_version'))
    aliases = {'deso': ('desokod','deso','deso_kod'), 'regso': ('regsokod','regso','regso_kod'),
               'municipality': ('kommunkod','kommunkod','kommun'), 'county': ('lanskod','länskod','lan'),
               'population_grid_1km': ('rut_id','ruta','rutinspire','id')}.get(kind, ())
    code = text(field('area_code', 'area_external_id', 'code')) or text(entry.get('area_code'))
    if code is None:
        lower = {str(k).casefold(): v for k, v in row.items()}
        code = next((text(lower[k]) for k in aliases if text(lower.get(k))), None)
    width = entry.get('code_width')
    if width and code and code.isdigit():
        code = code.zfill(int(width))
    if not kind or not code or not edition:
        raise ValueError('Area type, source code and boundary_version are required')
    return {'area_type': kind, 'area_external_id': code, 'boundary_version': edition}


def observations(row, entry, field):
    geo = geographic_fields(row, entry, field)
    period = text(field('period', 'year')) or text(entry.get('period'))
    if not period:
        raise ValueError('Statistics require an explicit observation period')
    dimensions = dict(entry.get('dimensions', {}))
    dimensions.update({k: json_value(row.get(v)) for k, v in entry.get('dimension_columns', {}).items()})
    source_dimensions = field('dimensions')
    if isinstance(source_dimensions, dict):
        dimensions.update(json_value(source_dimensions))
    indicators = entry.get('indicators')
    if indicators:
        specs = [(column, spec, row.get(column)) for column, spec in indicators.items()]
    else:
        source_field = entry.get('columns', {}).get('value', 'value')
        specs = [(source_field, {'indicator': text(field('indicator')) or entry.get('indicator'),
                                'unit': text(field('unit')) or entry.get('unit')}, field('value'))]
    result = []
    for source_field, spec, raw in specs:
        indicator, unit = text(spec.get('indicator')), text(spec.get('unit'))
        if not indicator or not unit:
            raise ValueError('Every statistical indicator needs its code and unit')
        raw_text = text(field('raw_value')) if not indicators else None
        raw_text = raw_text if raw_text is not None else text(raw)
        marker = entry.get('missing_values', {}).get(raw_text) if raw_text is not None else None
        if marker:
            value, status = None, str(marker)
        else:
            try:
                value = number(raw)
                status = 'available' if value is not None else 'missing'
            except (ValueError, TypeError):
                value, status = None, 'source_non_numeric'
        if not indicators and text(field('value_status')):
            status = text(field('value_status'))
        result.append(dict(geo, indicator=indicator, period=period, unit=unit,
                           value=value, raw_value=raw_text, value_status=status,
                           dimensions=dimensions, source_field=str(source_field), attributes={}))
    return result


def normalize_context(kind, row, geom, entry, field, attributes, source_record):
    import shapely
    if kind == 'statistics':
        evidence = observations(row, entry, field)
        for observation in evidence:
            observation['attributes'] = attributes
        return {'observations': evidence, 'attributes': attributes}
    if kind == 'areas':
        geo = geographic_fields(row, entry, field)
        parent_type = text(field('parent_type')) or text(entry.get('parent_type'))
        parent_code = text(field('parent_area_code', 'parent_external_id'))
        parent_version = text(field('parent_boundary_version')) or text(entry.get('parent_boundary_version'))
        if parent_code and (not parent_type or not parent_version):
            raise ValueError('A parent reference requires parent_type and parent_boundary_version')
        code = field('municipality_code', 'kommunkod', 'KOMMUNKOD')
        return dict(geo, name=text(field('name','namn','desonamn','regsonamn')),
                    municipality_code=municipality(code) if text(code) else None,
                    parent_type=parent_type,parent_external_id=parent_code,
                    parent_boundary_version=parent_version,geometry_valid=bool(shapely.is_valid(geom)),
                    observations=observations(row,entry,field) if entry.get('indicators') else [],attributes=attributes)
    return {'external_id': text(field('road_external_id')) or source_record,
            'source_layer': text(row.get('_source_layer')) or text(entry.get('layer')) or 'features',
            'source_member': text(row.get('_source_member')),
            'network_link_id': text(field('network_link_id','RLID','rlid','Rlid')),
            'start_measure': number(field('start_measure')), 'end_measure': number(field('end_measure')),
            'geometry_valid': bool(shapely.is_valid(geom)) if geom is not None else None,
            'attributes': attributes}
