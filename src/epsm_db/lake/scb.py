"""Download and normalize SCB vector/statistical data into immutable lake sources.

The PxWebApi v2 response and complete variable metadata are retained beside the
normalized rows. Current DeSO/RegSO editions are selected explicitly. Numeric
values, units, status codes, category codes/labels and observation years survive
normalization; nothing is apportioned to individual buildings.
"""
from datetime import datetime, timezone
import itertools
import json
import math
from pathlib import Path
import re
import shutil
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .progress import report

SCB_LAYERS = ('scb_population', 'scb_households', 'scb_housing_tenure', 'scb_income',
              'scb_employment', 'scb_education', 'scb_cars', 'scb_energy_use',
              'scb_heat_production', 'scb_electricity_production')


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


class Client:
    """Cached read-only requests, below SCB's published 30 calls/10 seconds."""
    def __init__(self):
        self.last_request = 0.0

    def download(self, url, destination, body=None):
        if destination.is_file():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        request_path = destination.with_suffix(destination.suffix + '.request.json')
        payload = json.dumps(body).encode() if body is not None else None
        headers = {'User-Agent': 'epsm_db/0.3 SCB data lake snapshot', 'Accept': '*/*'}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        for attempt in range(5):
            time.sleep(max(0, .5 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            temporary = destination.with_suffix(destination.suffix + '.partial')
            try:
                with urlopen(Request(url, data=payload, headers=headers), timeout=90) as response:
                    with temporary.open('wb') as stream:
                        shutil.copyfileobj(response, stream, length=1024 * 1024)
                    provenance = {'url': url, 'method': 'POST' if body is not None else 'GET',
                                  'body': body, 'retrieved_at': datetime.now(timezone.utc).isoformat(),
                                  'headers': {key: response.headers[key] for key in ('Content-Type','ETag','Last-Modified') if key in response.headers}}
                write_json(request_path, provenance)
                temporary.replace(destination)
                return
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                temporary.unlink(missing_ok=True)
                if isinstance(error, HTTPError) and error.code not in (429, 500, 502, 503, 504):
                    detail = error.read(2048).decode(errors='replace')
                    raise ValueError(f'SCB returned HTTP {error.code}: {url}: {detail}') from error
                if attempt == 4:
                    raise
                delay = min(30, 2 ** (attempt + 1))
                if isinstance(error, HTTPError):
                    try:
                        delay = min(60, max(delay, float(error.headers.get('Retry-After', delay))))
                    except ValueError:
                        pass
                report('scb_request_retry', url=url, seconds=delay, error=str(error))
                time.sleep(delay)

    def json(self, url, path, body=None):
        self.download(url, path, body)
        return json.loads(path.read_text())


def codes(dimension):
    index = dimension['category']['index']
    return list(index) if isinstance(index, list) else sorted(index, key=index.get)


def geography(code, administrative_version):
    match = re.fullmatch(r'(\d{4}[A-Z]\d{4})_DeSO(2025)', code)
    if match:
        return 'deso', match[1], match[2]
    match = re.fullmatch(r'(\d{4}R\d{3})_RegSO(2025)', code)
    if match:
        return 'regso', match[1], match[2]
    if code == '00':
        return 'country', 'SE', administrative_version
    if re.fullmatch(r'\d{2}', code):
        return 'county', code, administrative_version
    if re.fullmatch(r'\d{4}', code):
        return 'municipality', code, administrative_version
    # Unsuffixed DeSO/RegSO codes belong to older editions in these tables.
    return None


def normalize_response(data, table_id, source_file, administrative_version):
    dimensions = data['dimension']
    identifiers = data['id']
    axes = [codes(dimensions[key]) for key in identifiers]
    expected = math.prod(map(len, axes))
    values = data['value']
    if isinstance(values, list) and len(values) != expected:
        raise ValueError(f'{table_id}: response cell count does not match its dimensions')
    time_code, = data['role']['time']
    metric_code, = data['role']['metric']
    status = data.get('status') or {}
    for index, coordinate in enumerate(itertools.product(*axes)):
        keys = dict(zip(identifiers, coordinate))
        geo = geography(keys['Region'], administrative_version)
        if geo is None:
            raise ValueError(f'Unexpected geography in downloaded data: {keys["Region"]}')
        metric = keys[metric_code]
        unit = dimensions[metric_code]['category'].get('unit', {}).get(metric, {}).get('base')
        if not unit:
            raise ValueError(f'{table_id}: no published unit for {metric}')
        value = values[index] if isinstance(values, list) else values.get(str(index))
        source_status = status[index] if isinstance(status, list) else status.get(str(index))
        other = {key: val for key, val in keys.items() if key not in ('Region', time_code, metric_code)}
        category = dimensions[metric_code]['category']
        metric_extension = dimensions[metric_code].get('extension', {})
        yield {'area_type': geo[0], 'area_code': geo[1], 'boundary_version': geo[2],
               'period': keys[time_code], 'indicator': f'scb:{table_id}:{metric}',
               'unit': unit, 'value': value,
               'value_status': 'source_status:' + str(source_status) if source_status else ('available' if value is not None else 'missing'),
               'raw_value': str(source_status) if value is None and source_status else (str(value) if value is not None else None),
               'dimensions': other, 'scb_table_id': table_id,
               'indicator_label': category.get('label', {}).get(metric, metric),
               'dimension_labels': {key: dimensions[key]['category'].get('label', {}).get(val, val) for key, val in other.items()},
               'source_region_code': keys['Region'], 'source_response': source_file,
               'price_type': metric_extension.get('priceType', {}).get(metric),
               'reference_period': metric_extension.get('refperiod', {}).get(metric)}


def fetch_scb(catalogue, output):
    """Fetch selected national tables, retaining restartable request-sized files."""
    import pyogrio

    config = tomllib.loads(Path(catalogue).read_text())
    if config.get('scb_snapshot_version') != 1:
        raise ValueError('Expected scb_snapshot_version=1')
    if not config.get('tables') or any(not re.fullmatch(r'TAB[0-9]+', item.get('id','')) or not re.fullmatch(r'[a-z][a-z_]*', item.get('name','')) for item in config['tables']):
        raise ValueError('Every SCB table needs a TAB-number ID and a lowercase topic name')
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    saved_config = output / 'catalogue.json'
    if saved_config.exists() and json.loads(saved_config.read_text()) != config:
        raise ValueError('Catalogue changed. Choose a new snapshot directory.')
    write_json(saved_config, config)
    client = Client()
    base = config['api_base'].rstrip('/')
    api_config = client.json(base + '/config', output/'api-config.json')
    max_cells = min(config.get('max_cells', 100000), api_config['maxDataCells'])
    entries, inventory = [], []
    summary = {'state': 'running', 'tables': []}
    write_json(output/'download-status.json', summary)
    regso = config.get('regso')
    if regso:
        path = output/'boundaries/RegSO_2025.gpkg'
        client.download(regso['url'], path)
        available = pyogrio.list_layers(path)
        spatial = [str(layer) for layer, geom in available if geom is not None]
        if len(spatial) != 1:
            raise ValueError('Expected one spatial layer in the official RegSO download')
        info = pyogrio.read_info(path, layer=spatial[0])
        if info['crs'] != 'EPSG:3006' or info['features'] != regso['expected_features']:
            raise ValueError('RegSO download CRS/feature count differs from its declaration')
        fields = {str(name).casefold(): str(name) for name in info['fields']}
        mapping = {'area_code': fields['regsokod']}
        for dest, candidate in [('name','regsonamn'),('municipality_code','kommunkod')]:
            if candidate in fields:
                mapping[dest] = fields[candidate]
        entries.append({'name': 'scb-official-regso-2025', 'kind': 'areas',
                        'version': 'official-2025-snapshot', 'path': 'boundaries/RegSO_2025.gpkg',
                        'layer': spatial[0], 'area_type': 'regso', 'boundary_version': regso['boundary_version'],
                        'columns': mapping})
        inventory.append({'name': 'official_regso', 'features': info['features'], 'url': regso['url']})
        report('scb_boundary_downloaded', features=info['features'], layer=spatial[0])
    for spec in config['tables']:
        table_id = spec['id']
        directory = output/'raw'/table_id
        info = client.json(f'{base}/tables/{table_id}?lang=sv', directory/'table.json')
        metadata = client.json(f'{base}/tables/{table_id}/metadata?lang=sv', directory/'metadata.json')
        latest = info['lastPeriod']
        time_code, = metadata['role']['time']
        if latest not in codes(metadata['dimension'][time_code]):
            raise ValueError(f'{table_id}: latest period is absent from metadata')
        selected = {key: codes(dim) for key, dim in metadata['dimension'].items()}
        selected[time_code] = [latest]
        selected['Region'] = [code for code in selected['Region'] if geography(code, config['administrative_boundary_version'])]
        if not selected['Region']:
            raise ValueError(f'{table_id}: no compatible geography codes')
        cells_per_region = math.prod(len(v) for k, v in selected.items() if k != 'Region')
        chunk_size = max_cells // cells_per_region
        if chunk_size < 1:
            raise ValueError(f'{table_id}: one region exceeds the configured API cell limit')
        regions = selected['Region']
        total = 0
        report('scb_table_started', table_id=table_id, name=spec['name'], period=latest,
               regions=len(regions), expected_cells=len(regions)*cells_per_region)
        for chunk, start in enumerate(range(0, len(regions), chunk_size)):
            selection = dict(selected, Region=regions[start:start+chunk_size])
            body = {'selection': [{'variableCode': key, 'valueCodes': selection[key]} for key in metadata['id']]}
            raw_path = directory/f'part-{chunk:05d}.json'
            data = client.json(f'{base}/tables/{table_id}/data?lang=sv&outputFormat=json-stat2', raw_path, body)
            if data.get('updated') != metadata.get('updated'):
                raise ValueError(f'{table_id}: table changed during download; use a fresh snapshot')
            expected = math.prod(len(v) for v in selection.values())
            if math.prod(data['size']) != expected:
                raise ValueError(f'{table_id}: downloaded selection is incomplete')
            for key, chosen in selection.items():
                if set(codes(data['dimension'][key])) != set(chosen):
                    raise ValueError(f'{table_id}: response changed selected {key} codes')
            normalized = output/'normalized'/table_id/f'part-{chunk:05d}.jsonl'
            normalized.parent.mkdir(parents=True, exist_ok=True)
            if not normalized.exists():
                temporary = normalized.with_suffix('.jsonl.partial')
                with temporary.open('w') as stream:
                    for row in normalize_response(data, table_id, str(raw_path.relative_to(output)), config['administrative_boundary_version']):
                        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n')
                temporary.replace(normalized)
            total += expected
            report('scb_chunk_downloaded', table_id=table_id, chunk=chunk, cells=expected, completed_cells=total)
        # Cached metadata is a snapshot. A live check catches a mid-download release.
        check_path = directory/'end-table.json'
        check_path.unlink(missing_ok=True)
        final_info = client.json(f'{base}/tables/{table_id}?lang=sv', check_path)
        if final_info['updated'] != info['updated']:
            raise ValueError(f'{table_id}: changed while being downloaded; use a fresh snapshot')
        entries.append({'name': 'scb-' + spec['name'] + '-api-v2', 'kind': 'statistics',
                        'version': info['updated']+'-period-'+latest,
                        'path': f'normalized/{table_id}/*.jsonl'})
        item = {'name': spec['name'], 'table_id': table_id, 'title': info['label'],
                'period': latest, 'regions': len(regions), 'observations': total,
                'source_updated': info['updated'], 'metadata': str((directory/'metadata.json').relative_to(output)),
                'units': metadata['dimension'][metadata['role']['metric'][0]]['category'].get('unit', {})}
        inventory.append(item)
        summary['tables'].append(item)
        write_json(output/'download-status.json', summary)
        report('scb_table_complete', **item)
    lines = ['schema_version = 1', 'root = "."', 'srid = 3006', '']
    for entry in entries:
        lines.append('[[sources]]')
        for key, value in entry.items():
            if not isinstance(value, dict):
                lines.append(key + ' = ' + json.dumps(value, ensure_ascii=False))
        if entry.get('columns'):
            lines.append('[sources.columns]')
            lines.extend(key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in entry['columns'].items())
        lines.append('')
    manifest = output/'manifest.toml'
    manifest.write_text('\n'.join(lines))
    write_json(output/'inventory.json', {'license': api_config['license'], 'sources': inventory})
    summary.update(state='complete', manifest=str(manifest), completed_at=datetime.now(timezone.utc).isoformat())
    write_json(output/'download-status.json', summary)
    return summary
