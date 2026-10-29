"""Bounded vector reads, including the GeoPackages inside an NVDB ZIP archive."""
from contextlib import ExitStack
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from .progress import report


def vector_rows(path, entry, skip=0):
    import pyogrio
    position = 0
    boundary_layer_found = False
    with ExitStack() as stack:
        datasets = [('', path)]
        if path.suffix.lower() == '.zip':
            archive = stack.enter_context(ZipFile(path))
            members = sorted((m for m in archive.infolist()
                              if not m.is_dir() and m.filename.lower().endswith('.gpkg')),
                             key=lambda m: m.filename)
            if not members:
                raise ValueError(f'ZIP contains no GeoPackage files: {path}')
            temporary = Path(stack.enter_context(TemporaryDirectory(prefix='epsm-nvdb-')))
            datasets = []
            for index, member in enumerate(members):
                # Use an owned basename; never extract archive-controlled paths.
                destination = temporary / f'dataset-{index}.gpkg'
                report('unpack_geopackage', archive=path.name, member=member.filename)
                with archive.open(member) as source, destination.open('wb') as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                datasets.append((member.filename, destination))
        for member, dataset in datasets:
            available = sorted((str(name), str(kind) if kind is not None else None)
                               for name, kind in pyogrio.list_layers(dataset))
            if entry.get('layers') == 'all':
                selected = available
            elif entry.get('layer'):
                selected = [item for item in available if item[0] == entry['layer']]
                if not selected:
                    raise ValueError(f"Layer {entry['layer']!r} not found in {path.name}")
            elif len(available) == 1:
                selected = available
            else:
                raise ValueError(f'{path.name} has multiple layers; declare layer or layers="all"')
            for layer, geometry_type in selected:
                if entry['kind'] == 'areas' and geometry_type is None:
                    report('source_layer_skipped', layer=layer, geometry_type=geometry_type)
                    continue
                if entry['kind'] == 'areas':
                    boundary_layer_found = True
                info = pyogrio.read_info(dataset, layer=layer)
                count = info.get('features', -1)
                if count >= 0 and position + count <= skip:
                    position += count
                    continue
                local_skip = max(0, skip - position) if count >= 0 else 0
                position += local_skip
                with pyogrio.open_arrow(dataset, layer=layer, return_fids=True,
                        use_pyarrow=True, skip_features=local_skip, batch_size=4096) as (meta, reader):
                    if geometry_type is not None and str(meta.get('crs')).upper() != 'EPSG:3006':
                        raise ValueError(f'Expected EPSG:3006: {path.name}, layer {layer}')
                    geometry_column = meta.get('geometry_name') or 'wkb_geometry'
                    fid_column = meta.get('fid_column')
                    for batch in reader:
                        for row in batch.to_pylist():
                            position += 1
                            if position <= skip:
                                continue
                            if fid_column:
                                row['_fid'] = row.pop(fid_column)
                            geometry = row.pop(geometry_column, None)
                            row['_source_layer'] = layer
                            row['_source_member'] = member
                            yield position, row, geometry
    if entry['kind']=='areas' and not boundary_layer_found:
        raise ValueError(f'No spatial boundary layers were found in {path.name}')
