import json
import pytest
from scripts import accept_loader_benchmark as gate
from sweden_ubem.hashing import sha256_file


@pytest.fixture
def correction(tmp_path, monkeypatch):
    baseline='sql/baselines/0.0.4/001_compact.sql'
    for directory,cast in [('new-code',''),('new-code-lineage-fixed','::text')]:
        root=tmp_path/directory
        (root/baseline).parent.mkdir(parents=True)
        (root/baseline).write_text("'object_version',l.object_version"+cast+",'outline'")
        (root/'loader.py').write_text('unchanged loader')
    lineage=tmp_path/'samples/1048576/register/source-lineage.parquet'
    lineage.parent.mkdir(parents=True)
    lineage.write_bytes(b'unchanged source')
    expected=dict(rows=10,multiset_sha256='historical')
    observed=dict(rows=10,multiset_sha256='numeric')
    old={'facts':{'lineage':expected,'components':{'rows':10}}}
    new={'facts':{'lineage':observed,'components':{'rows':10}}}
    for name,result in [('old',old),('new',new)]:
        (tmp_path/f'{name}-1048576-final.json').write_text(json.dumps(result))
    environment={'application_digest':'original'}
    receipt=dict(accepted=True,original_code_digest='original',corrected_code_digest='corrected',
                 source_projection_digest='unchanged-projection',
                 old_benchmark_sha256=sha256_file(tmp_path/'old-1048576-final.json'),
                 new_benchmark_sha256=sha256_file(tmp_path/'new-1048576-final.json'),
                 source_lineage_sha256=sha256_file(lineage),original_lineage=observed,
                 expected_lineage=expected,corrected_lineage=expected,
                 original_differing_groups=['lineage'])
    path=tmp_path/'correction.json'
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(gate,'application_digest',lambda:'corrected')
    monkeypatch.setattr(gate,'source_projection_digest',lambda:'unchanged-projection')
    return tmp_path,path,environment,old,new


def test_accepts_only_verified_display_cast(correction):
    assert gate.verify_view_correction(*correction)['accepted']


def test_epc_budget_counts_parsed_records_and_preserves_archives(tmp_path):
    root=tmp_path/'epc'
    data=root/'fore_v260/data';data.mkdir(parents=True)
    path=data/'rows.txt'
    path.write_text('FormularId|IdLankod|IdKommunkod|IdFastBet|IdHusnr\n1|14|85|"first\nsecond"|1\n2|14|85|other|2\n')
    document=root/'fore_v260/spec.pdf';document.write_bytes(b'original document')
    archive=root/'fore_v260/retained.parquet';archive.write_bytes(b'large retained archive'*100)
    budget=gate.epc_input_budget(root)
    assert budget['source_rows']==2
    assert budget['input_bytes']==path.stat().st_size+document.stat().st_size
    assert budget['retained_archive_bytes']==archive.stat().st_size
    assert archive.exists()


@pytest.mark.parametrize('change',['loader','second_cast','source','evidence','digest'])
def test_rejects_unmeasured_or_unverified_change(correction,change):
    base,path,environment,old,new=correction
    if change=='loader':
        (base/'new-code-lineage-fixed/loader.py').write_text('different loader')
    elif change=='second_cast':
        with (base/'new-code-lineage-fixed/sql/baselines/0.0.4/001_compact.sql').open('a') as stream:
            stream.write(' ALTER TABLE extra')
    elif change=='source':
        (base/'samples/1048576/register/source-lineage.parquet').write_bytes(b'changed source')
    elif change=='evidence':
        new['facts']['components']['rows']=9
    else:
        receipt=json.loads(path.read_text());receipt['corrected_code_digest']='unverified'
        path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        gate.verify_view_correction(base,path,environment,old,new)
