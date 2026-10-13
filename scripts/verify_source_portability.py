"""Move the whole bundle temporarily; verify with its former root unavailable."""
import argparse
import json
import os
from pathlib import Path
from sweden_ubem.asset_operations import rename_exclusive,publish_json
from sweden_ubem.bundle import verify_bundle,translated_reference
from sweden_ubem.dataset import LocalSources


def verify(root,output):
    root=Path(root).resolve()
    moved=root.with_name(root.name+'.portability-check')
    if moved.exists():
        raise FileExistsError('Portability destination already exists; inspect interrupted operation')
    old_env=os.environ.get('UBEM_SOURCE_ROOT')
    rename_exclusive(root,moved)
    try:
        if root.exists():raise RuntimeError('Old root remains available')
        os.environ['UBEM_SOURCE_ROOT']=str(root)
        sources=LocalSources(root=moved)
        existing=[]
        for item in sources.inventory.sources:
            if item.role=='application_input':continue
            path=sources.path(item.key)
            if not path.is_relative_to(moved):raise ValueError(f'Nonportable source: {item.key}')
            if path.exists():existing.append(item.key)
        result=verify_bundle(moved)
        events=moved/'site-events/assertions/uddevalla-epc-site-events.json'
        documents=[translated_reference(events,e['source_document'],source_root=moved,
                    expected_sha256=e['source_document_sha256']) for e in json.loads(events.read_text())['events']]
        result.update(old_root_unavailable=True,explicit_root_overrode_environment=True,
                      resolved_inventory_keys=existing,event_documents_verified=len(documents))
        publish_json(output,result)
        return result
    finally:
        rename_exclusive(moved,root)
        if old_env is None:os.environ.pop('UBEM_SOURCE_ROOT',None)
        else:os.environ['UBEM_SOURCE_ROOT']=old_env


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(verify(a.source_root,a.output)))
