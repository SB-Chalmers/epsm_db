"""Resolve explicitly declared SCB snapshots without workstation fallbacks."""
from pathlib import Path


def snapshot_paths(sources, bundle):
    explicit = bundle.get('scb_snapshot_paths')
    if explicit is None:
        return [sources.path(key) for key in bundle.get('scb_snapshot_keys', ['scb_snapshot_uddevalla'])]
    if 'scb_snapshot_keys' in bundle:
        raise ValueError('Declare SCB snapshot paths or inventory keys, not both')
    if not isinstance(explicit, list) or not explicit:
        raise ValueError('scb_snapshot_paths must be a nonempty list')
    root = sources.root.resolve()
    paths = []
    for value in explicit:
        relative = Path(value)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('SCB snapshot path must be bundle-relative')
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('SCB snapshot path escapes the bundle')
        if path in paths:
            raise ValueError('SCB snapshot paths must be unique')
        paths.append(path)
    return paths
