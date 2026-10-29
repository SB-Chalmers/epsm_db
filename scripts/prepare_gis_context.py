#!/usr/bin/env python3
"""Prepare supplied SCB context sources without fetching data or modifying originals.

National administrative boundaries are coarse SCB map boundaries. RegSO 2025 is
explicitly derived by dissolving the delivered DeSO 2025 polygons by regsokod.
Only the municipalities in existing income snapshots have income observations.
"""
import argparse
import json
from pathlib import Path
import re
import tempfile
from zipfile import ZipFile


def main():
    import geopandas as gpd
    import pyogrio
    import shapely

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--national-root', required=True, type=Path)
    parser.add_argument('--deso', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Output already exists. Reuse the immutable prepared files, or choose a fresh directory.')
    args.output.mkdir(parents=True)
    sources = []
    provenance = []

    def area(name, path, kind, edition, code, **extra):
        sources.append(dict(name=name, version=extra.pop('version', edition), kind='areas', path=path,
                            area_type=kind, boundary_version=edition, columns={'area_code': code, **extra.pop('columns', {})}, **extra))

    scb = args.national_root / 'scb'
    archive = scb / 'boundaries/shape_svenska_260225.zip'
    counties = None
    with ZipFile(archive) as outer, tempfile.TemporaryDirectory(prefix='epsm-scb-') as temporary:
        for member, kind, code, label in [('LanSweref99TM.zip','county','LnKod','LnNamn'),
                                          ('Kommun_Sweref99TM.zip','municipality','KnKod','KnNamn')]:
            inner = Path(temporary) / member
            inner.write_bytes(outer.read(member))
            frame = pyogrio.read_dataframe('/vsizip/' + str(inner)).to_crs(3006)
            frame['source_archive'] = str(archive)
            frame['source_member'] = member
            frame['geometry_origin'] = 'supplied SCB administrative overview boundary'
            if kind == 'municipality':
                frame['municipality_code'] = frame[code]
            filename = kind + '.parquet'
            frame.to_parquet(args.output / filename, index=False)
            area('scb-' + kind, filename, kind, 'source-260225', code, columns={'name': label})
            provenance.append({'prepared': filename, 'source': str(archive), 'member': member, 'features': len(frame)})
            if kind == 'county':
                counties = frame
    country = gpd.GeoDataFrame({'area_code': ['SE'], 'name': ['Sweden'],
                               'geometry_origin': ['dissolved supplied SCB county overview polygons'],
                               'source_archive': [str(archive)]},
                              geometry=[shapely.union_all(counties.geometry.values)], crs=3006)
    country.to_parquet(args.output / 'country.parquet', index=False)
    area('scb-country-derived', 'country.parquet', 'country', 'source-260225', 'area_code', columns={'name': 'name'})
    deso = pyogrio.read_dataframe(args.deso, layer='DeSO_2025')
    regso = deso[['regsokod', 'kommunkod', 'geometry']].dissolve(by='regsokod', as_index=False, aggfunc='first')
    regso['geometry_origin'] = 'derived: DeSO_2025 dissolved by source regsokod'
    regso['source_path'] = str(args.deso)
    regso['source_version'] = '2025_v1'
    regso.to_parquet(args.output / 'regso-derived.parquet', index=False)
    area('regso-2025-from-deso', 'regso-derived.parquet', 'regso', '2025', 'regsokod', columns={'municipality_code': 'kommunkod'})
    provenance.append({'prepared': 'regso-derived.parquet', 'source': str(args.deso), 'features': len(regso),
                       'method': 'union of DeSO polygons grouped by source regsokod; not a separately supplied official RegSO layer'})
    population = scb / 'population/befolkning_1km_2025.gpkg'
    frame = pyogrio.read_dataframe(population, layer='befolkning_1km_2025')
    fields = ['beftotalt','man','kvinna'] + [c for c in frame.columns if c.startswith('ald')]
    if set(frame['referenstid'].dropna().astype(str)) != {'20251231'}:
        raise ValueError('Population reference dates differ from the declared observation period')
    frame['source_path'] = str(population)
    frame.to_parquet(args.output / 'population-grid.parquet', index=False)
    area('scb-population-grid-2025','population-grid.parquet','population_grid_1km','2025','rutid_inspire',
         columns={'period': 'referenstid'},
         indicators={c: {'indicator': 'population:' + c, 'unit': 'persons'} for c in fields})
    provenance.append({'prepared': 'population-grid.parquet', 'source': str(population), 'features': len(frame),
                       'period': '20251231', 'note': 'Original disclosed values retained; categories/totals may not add up.'})
    observations = {}
    for manifest in sorted((scb / 'snapshots').glob('*/manifest.json')):
        meta = json.loads(manifest.read_text())
        response_path = manifest.parent / meta['income']['response_file']
        response = json.loads(response_path.read_text())
        dimensions = [c['code'] for c in response['columns'] if c['type'] != 'c']
        contents = [c for c in response['columns'] if c['type'] == 'c']
        for record in response['data']:
            keys = dict(zip(dimensions, record['key']))
            match = re.fullmatch(r'(\d{4}R\d+)_RegSO(\d{4})', keys['Region'])
            if not match:
                raise ValueError('Unrecognized RegSO code/edition: ' + keys['Region'])
            dims = {k: v for k, v in keys.items() if k not in ('Region', 'Tid')}
            for content, value in zip(contents, record['values']):
                if content['text'] != 'Medianvärde, tkr':
                    raise ValueError('Unrecognized income unit: ' + content['text'])
                identity = (match[1], match[2], keys['Tid'], content['code'], json.dumps(dims, sort_keys=True))
                row = {'area_code': match[1], 'boundary_version': match[2], 'period': keys['Tid'],
                       'indicator': content['code'], 'unit': 'tkr', 'value': value, 'dimensions': dims,
                       'source_response': str(response_path), 'source_manifest': str(manifest)}
                if identity in observations and observations[identity]['value'] != value:
                    raise ValueError('Conflicting cached income snapshots for the same observation')
                observations[identity] = row
    with (args.output / 'income.jsonl').open('w') as output:
        for key in sorted(observations):
            output.write(json.dumps(observations[key], ensure_ascii=False) + '\n')
    sources.append({'name': 'scb-cached-regso-income', 'version': 'delivered-2026-10-13', 'kind': 'statistics',
                    'path': 'income.jsonl', 'area_type': 'regso'})
    lines = ['schema_version = 1', 'root = "."', 'srid = 3006', '']
    for entry in sources:
        lines.append('[[sources]]')
        for key, value in entry.items():
            if isinstance(value, dict):
                continue
            lines.append(key + ' = ' + json.dumps(value, ensure_ascii=False))
        for key, value in entry.get('columns', {}).items():
            if key == next(iter(entry['columns'])):
                lines.append('[sources.columns]')
            lines.append(key + ' = ' + json.dumps(value, ensure_ascii=False))
        for field, spec in entry.get('indicators', {}).items():
            lines.append('[sources.indicators.' + field + ']')
            lines.extend(k + ' = ' + json.dumps(v, ensure_ascii=False) for k, v in spec.items())
        lines.append('')
    (args.output / 'manifest.toml').write_text('\n'.join(lines))
    (args.output / 'provenance.json').write_text(json.dumps({'sources': provenance, 'income_observations': len(observations)}, indent=2)+'\n')
    print(json.dumps({'manifest': str(args.output / 'manifest.toml'), 'regso_areas': len(regso),
                      'population_cells': len(frame), 'income_observations': len(observations)}))


if __name__ == '__main__':
    main()
