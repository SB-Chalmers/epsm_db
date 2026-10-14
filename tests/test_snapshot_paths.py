from pathlib import Path
from types import SimpleNamespace
import pytest
from sweden_ubem.snapshot_paths import snapshot_paths


def test_portable_paths_move_with_source_root(tmp_path):
    sources = SimpleNamespace(root=tmp_path, path=lambda key: tmp_path/key)
    bundle = {'scb_snapshot_paths': ['scb/snapshots/1485/a', 'scb/snapshots/1480/b']}
    assert snapshot_paths(sources, bundle) == [tmp_path/'scb/snapshots/1485/a', tmp_path/'scb/snapshots/1480/b']
    sources.root = tmp_path/'different-root'
    assert snapshot_paths(sources,bundle)[0] == sources.root/'scb/snapshots/1485/a'
    assert snapshot_paths(sources,{'scb_snapshot_keys':['old-key']}) == [tmp_path/'old-key']


@pytest.mark.parametrize('bundle',[
    {'scb_snapshot_paths':['/tmp/source']}, {'scb_snapshot_paths':['../source']},
    {'scb_snapshot_paths':['same','same']}, {'scb_snapshot_paths':[]},
    {'scb_snapshot_paths':['a'],'scb_snapshot_keys':['key']},
])
def test_reject_conflicting_or_escaping_declarations(tmp_path,bundle):
    with pytest.raises(ValueError):
        snapshot_paths(SimpleNamespace(root=tmp_path),bundle)


def test_reject_directory_symlink_escape(tmp_path):
    root=tmp_path/'root';root.mkdir()
    (root/'link').symlink_to(tmp_path/'elsewhere',target_is_directory=True)
    with pytest.raises(ValueError,match='escapes'):
        snapshot_paths(SimpleNamespace(root=root),{'scb_snapshot_paths':['link/a']})
