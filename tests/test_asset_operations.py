import pytest
from sweden_ubem.asset_operations import identity, relocate, deletion_manifest, delete_listed


def test_interrupted_publish_resumes_and_preserves_note(tmp_path):
    old = tmp_path / "old/input.txt"
    old.parent.mkdir()
    old.write_bytes(b"original source")
    root = tmp_path / "bundle"
    entry = {"original": str(old), "destination": "sources/input.txt", "identity": identity(old)}
    def interrupt():
        raise RuntimeError("interrupted after publish")
    with pytest.raises(RuntimeError):
        relocate(entry, root, after_publish=interrupt)
    result = relocate(entry, root)
    assert result.read_bytes() == b"original source"
    assert not old.exists()
    assert old.with_name("input.txt.RELOCATED.txt").is_file()


def test_conflicting_destination_and_changed_source_are_not_deleted(tmp_path):
    old = tmp_path / "input"
    old.write_text("original")
    root = tmp_path / "bundle"
    root.mkdir()
    entry = {"original": str(old), "destination": "input", "identity": identity(old)}
    (root / "input").write_text("conflict")
    with pytest.raises(ValueError, match="Conflicting"):
        relocate(entry, root)
    assert old.read_text() == "original"
    old.write_text("changed")
    with pytest.raises(ValueError, match="identity changed"):
        relocate(entry, root)


def test_cleanup_refuses_protected_unlisted_and_changed_targets(tmp_path):
    protected = tmp_path / "sources"
    protected.mkdir()
    with pytest.raises(ValueError, match="Protected"):
        deletion_manifest([tmp_path], protected=[protected])
    disposable = tmp_path / "disposable"
    disposable.mkdir()
    item = disposable / "file"
    item.write_text("generated")
    manifest = deletion_manifest([disposable], protected=[protected])
    with pytest.raises(ValueError, match="unlisted"):
        delete_listed(manifest, [item])
    item.write_text("different")
    with pytest.raises(ValueError, match="identities"):
        delete_listed(manifest, [disposable])
    manifest = deletion_manifest([disposable], protected=[protected])
    delete_listed(manifest, [disposable])
    assert protected.is_dir() and not disposable.exists()


def test_relocation_refuses_symlink_escape(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    old = tmp_path / "input"
    old.write_text("source")
    with pytest.raises(ValueError, match="escapes"):
        relocate({"original": str(old), "destination": "outside/new", "identity": identity(old)}, root)


def test_cross_filesystem_copy_resume_and_distinct_aliases(tmp_path,monkeypatch):
    import sweden_ubem.asset_operations as operations
    monkeypatch.setattr(operations,"same_filesystem",lambda source,parent:False)
    first=tmp_path/"first.txt"
    second=tmp_path/"second.txt"
    first.write_bytes(b"identical source bytes")
    second.write_bytes(first.read_bytes())
    root=tmp_path/"bundle"
    entries=[dict(original=str(p),destination="input.txt",identity=identity(p)) for p in (first,second)]
    def interrupt():raise RuntimeError("interrupted before source removal")
    with pytest.raises(RuntimeError,match="source removal"):
        relocate(entries[0],root,after_publish=interrupt)
    assert first.exists() and (root/"input.txt").exists()
    for entry in entries:relocate(entry,root)
    assert not first.exists() and not second.exists()
    assert (root/"input.txt").read_bytes()==b"identical source bytes"
    assert first.with_name(first.name+".RELOCATED.txt").exists()
    assert second.with_name(second.name+".RELOCATED.txt").exists()
