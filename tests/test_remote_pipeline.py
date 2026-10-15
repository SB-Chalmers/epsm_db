import io
import json
import tarfile
import pytest
from scripts.remote_pipeline import Pipeline,GateFailure,safe_member,verify_transport,sha256,check_capacity
from scripts.remote_enrichment import clone_sources,historical_replace


def test_status_publication_supports_concurrent_writers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from scripts.remote_pipeline import publish
    target = tmp_path / 'status.json'
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: publish(target, {'writer': i, 'payload': 'x' * 10000}), range(100)))
    value = json.loads(target.read_text())
    assert value['writer'] in range(100)
    assert value['payload'] == 'x' * 10000
    assert list(tmp_path.glob('*.tmp')) == []


@pytest.mark.parametrize('name',['/etc/file','../outside','inside/../../outside'])
def test_archive_rejects_escape(tmp_path,name):
    with pytest.raises(GateFailure):safe_member(tarfile.TarInfo(name),tmp_path,set())


def test_archive_rejects_links_and_special_devices(tmp_path):
    member=tarfile.TarInfo('link');member.type=tarfile.SYMTYPE;member.linkname='inside'
    with pytest.raises(GateFailure):safe_member(member,tmp_path,set())
    member.type=tarfile.LNKTYPE;member.linkname='../outside'
    with pytest.raises(GateFailure):safe_member(member,tmp_path,{'../outside'})
    member.linkname='file'
    assert safe_member(member,tmp_path,{'file'})==tmp_path/'link'


def test_delivery_checksum_and_count_are_required(tmp_path):
    asset=tmp_path/'asset';asset.write_bytes(b'original')
    manifest=tmp_path/'transport-file-manifest.json'
    manifest.write_text(json.dumps({'files':[dict(path='asset',bytes=8,sha256=sha256(asset))]}))
    assert verify_transport(tmp_path,2)['file_occurrences']==2
    with pytest.raises(GateFailure):verify_transport(tmp_path,1)
    asset.write_bytes(b'changed!')
    with pytest.raises(GateFailure):verify_transport(tmp_path,2)


def test_gate_failure_has_no_pass_receipt_and_config_change_cannot_resume(tmp_path):
    config=dict(root=str(tmp_path),release_id='test',reserve_bytes=1)
    pipeline=Pipeline(config)
    def fail():raise GateFailure('failed validation')
    with pytest.raises(GateFailure):pipeline.step('validation',fail)
    assert not (pipeline.receipts/'validation.json').exists()
    assert pipeline.step('validation',lambda:{'ok':True})=={'ok':True}
    second=Pipeline({**config,'reserve_bytes':2})
    with pytest.raises(GateFailure,match='conflicts'):second.step('validation',lambda:None)


def test_restore_disconnection_checks_directory_identity(tmp_path):
    pipeline=Pipeline(dict(root=str(tmp_path),release_id='test',reserve_bytes=1))
    root=pipeline.release/'sources';(root/'manifests').mkdir(parents=True)
    (root/'manifests/file-inventory.json').write_text('{}')
    pipeline.env['UBEM_SOURCE_ROOT']=str(root)
    pipeline.recover_disconnected_sources()
    hidden=root.with_name('sources.acceptance-disconnected');root.rename(hidden)
    pipeline.recover_disconnected_sources();assert root.exists()
    root.rename(hidden);(hidden/'manifests/file-inventory.json').write_text('changed')
    with pytest.raises(GateFailure,match='identity changed'):pipeline.recover_disconnected_sources()


def test_enrichment_does_not_modify_hardlinked_original(tmp_path):
    original=tmp_path/'original';(original/'manifests').mkdir(parents=True)
    asset=original/'prepared/heights/catalogue';asset.parent.mkdir(parents=True);asset.write_bytes(b'old')
    inventory=dict(files=[dict(path='prepared/heights/catalogue',original='/upstream/catalogue',bytes=3,sha256=sha256(asset))])
    (original/'manifests/file-inventory.json').write_text(json.dumps(inventory))
    working=tmp_path/'working';clone_sources(original,working)
    fresh=tmp_path/'fresh';fresh.write_bytes(b'new')
    historical_replace(working,'prepared/heights/catalogue',fresh)
    assert asset.read_bytes()==b'old'
    assert (working/'prepared/heights/catalogue').read_bytes()==b'new'
    rows=json.loads((working/'manifests/file-inventory.json').read_text())['files']
    assert any(r['original']=='/upstream/catalogue' and 'history' in r['path'] for r in rows)


def test_delivery_verifies_nested_transport_before_publishing(tmp_path, monkeypatch):
    import scripts.remote_pipeline as module
    config = dict(root=str(tmp_path), release_id='test-release', reserve_bytes=1,
                  uncompressed_bytes=8, remote_archive=str(tmp_path/'archive'),
                  archive_sha256='verified-archive', archive_bytes=10, file_occurrences=2)
    pipeline = Pipeline(config)
    (tmp_path/'releases').mkdir()
    def extracted(archive, staging):
        payload = staging/'test-release'
        payload.mkdir(parents=True)
        (payload/'asset').write_bytes(b'original')
        (payload/'transport-file-manifest.json').write_text(json.dumps({'files': [
            dict(path='asset', bytes=8, sha256=sha256(payload/'asset'))]}))
    monkeypatch.setattr(module, 'extract_archive', extracted)
    receipt = pipeline.delivery()
    assert receipt['file_occurrences'] == 2
    assert (pipeline.release/'asset').read_bytes() == b'original'
    assert (pipeline.release/'delivery-receipt.json').is_file()
    assert not list((tmp_path/'releases').glob('.staging-*'))
    assert pipeline.delivery() == receipt


@pytest.mark.parametrize('invalid', ['missing_root', 'symlink_root', 'extra_root', 'count', 'checksum'])
def test_invalid_nested_transport_never_publishes(tmp_path, monkeypatch, invalid):
    import scripts.remote_pipeline as module
    pipeline = Pipeline(dict(root=str(tmp_path), release_id='release', reserve_bytes=1,
                             uncompressed_bytes=8, remote_archive=str(tmp_path/'archive'),
                             archive_sha256='archive', archive_bytes=10, file_occurrences=2))
    (tmp_path/'releases').mkdir()
    def extracted(archive, staging):
        staging.mkdir()
        payload = staging/'release'
        if invalid == 'missing_root':
            return
        if invalid == 'symlink_root':
            payload.symlink_to(tmp_path, target_is_directory=True)
            return
        payload.mkdir()
        asset = payload/'asset'
        asset.write_bytes(b'original')
        rows = [] if invalid == 'count' else [dict(path='asset', bytes=8,
                sha256='invalid' if invalid == 'checksum' else sha256(asset))]
        (payload/'transport-file-manifest.json').write_text(json.dumps({'files': rows}))
        if invalid == 'extra_root':
            (staging/'unexpected').write_text('unpublished')
    monkeypatch.setattr(module, 'extract_archive', extracted)
    with pytest.raises(GateFailure):
        pipeline.delivery()
    assert not pipeline.release.exists()
    assert not list(pipeline.receipts.glob('*.json'))


@pytest.mark.parametrize('path', ['../escape', '/absolute'])
def test_transport_rejects_unsafe_manifest_paths(tmp_path, path):
    (tmp_path/'transport-file-manifest.json').write_text(json.dumps({'files': [
        dict(path=path, bytes=1, sha256='irrelevant')]}))
    with pytest.raises(GateFailure, match='Unsafe manifest path'):
        verify_transport(tmp_path, 2)
