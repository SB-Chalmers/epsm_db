"""Isolated old/new loader run over the same prepared real-source sample."""
import argparse
from collections import Counter
import hashlib
import gzip
import json
import os
from pathlib import Path
import resource
import sqlite3
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo


class SampleSources:
    profile='synthetic'
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.inventory=SimpleNamespace(_by_key={'height_staging_catalogue':SimpleNamespace(expected_sha256=None)})
        self.paths=dict(tower_component_register_root=self.root/'register',lm_building_geometry_primary=self.root/'buildings.gpkg',
            lm_property_geometry=self.root/'properties.gpkg',building_registry_locator=self.root/'locator.sqlite',
            lm_building_xml_root=self.root/'xml',lm_address_xml_root=self.root/'addresses',epc_raw_root=self.root/'epc',
            height_staging_catalogue=self.root/'heights.parquet',height_staging_reconciliation=self.root/'reconciliation.json',
            height_path_mapping=self.root/'height-paths.json')
    def path(self,key):return self.paths.get(key,self.root/'missing'/key)
    def components(self):return self.path('tower_component_register_root')


def metrics(c):
    relations=c.execute("SELECT n.nspname||'.'||r.relname,pg_table_size(r.oid),pg_indexes_size(r.oid) FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE r.relkind='r' AND n.nspname IN ('catalog','raw_lm','raw_height','raw_epc','raw_scb','stock','evidence','context','scope','quality') ORDER BY 1").fetchall()
    temporary=c.execute('SELECT temp_files,temp_bytes FROM pg_stat_database WHERE datname=current_database()').fetchone()
    return dict(database_bytes=c.execute('SELECT pg_database_size(current_database())').fetchone()[0],
        heap_toast_bytes=sum(r[1] for r in relations),index_bytes=sum(r[2] for r in relations),relations=relations,
        wal_lsn=c.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0],temporary_files=temporary[0],temporary_bytes=temporary[1])


def signatures(url,output,dump_facts=False):
    # Compare consumer facts, not the deliberately removed original-record copies.
    tables={
      'buildings':('stock.building', ['building_id','municipality_code','municipality_values','processing_partition','object_status','status_verified','is_existing','house_number','construction_year','main_building']),
      'components':('stock.geometry_component',['component_id','building_id','geometry_hash','geom','outline_labels','representation_status','owner_tile_id','source_fids','revision_timestamp','object_version']),
      'lineage':('stock.component_source_feature',['component_id','source_fid','disposition','original_values']),
      'purposes':('stock.building_purpose',['building_id','ordinal','category','detail','is_main','original_values']),
      'entrances':('stock.entrance',['entrance_id','building_id','address_place_id','original_values']),
      'addresses':('stock.address_place',['address_place_id','street_name','address_number','locality','match_key','base_match_key','geom']),
      'property_parts':('stock.property_area_part',['property_area_part_id','property_unit_id','geom']),
      'property_designations':('stock.property_designation',['property_unit_id','municipality_code','raw_label','normalized_label']),
      'heights':('evidence.component_height',['component_id','input_geometry_hash','height_status','representative_height_m','eave_height_m','ridge_height_m','roof_form','details','evidence_sha256']),
      'height_catalogue':('raw_height.record',['source_record','original_values']),
      'epc_before':('raw_epc.before_v260_row',['source_file','source_record','original_values','certificate_id','subject_id']),
      'epc_after':('raw_epc.after_v260_row',['source_file','source_record','original_values','certificate_id','subject_id']),
      'epc_subjects':('evidence.epc_subject',['subject_id','certificate_id','municipality_code','designation_normalized','house_number','original_values']),
      'epc_observations':('evidence.epc_observation',['observation_id','certificate_id','owner_kind','owner_key','source_field','raw_values','normalized_value','source_unit','normalized_unit','value_status','mapping_status','source_locators'])}
    result={}
    cache=Path(output).with_suffix('.comparison.sqlite')
    with psycopg.connect(url,options='-c default_transaction_read_only=on') as c,sqlite3.connect(cache) as store:
        # Verification runs outside ingestion timing. A bounded cache avoids
        # random disk reads for every digest in the large exact multiset audit.
        store.execute('PRAGMA cache_size=-262144')
        store.execute('CREATE TABLE counts(digest BLOB PRIMARY KEY,n INTEGER NOT NULL) WITHOUT ROWID')
        for name,(table,columns) in tables.items():
            dump=gzip.open(str(output)+'.'+name+'.jsonl.gz','wt') if dump_facts and name in {'property_designations','heights','height_catalogue'} else None
            entries=[]
            for field in columns:
                entries.append(sql.Literal(field))
                entries.append(sql.SQL("encode(ST_AsEWKB({},'XDR'),'hex')").format(sql.Identifier(field)) if field=='geom' else sql.Identifier(field))
            statement=sql.SQL('SELECT jsonb_build_object({}) FROM {}').format(sql.SQL(',').join(entries),sql.Identifier(*table.split('.')))
            if name=='height_catalogue':statement+=sql.SQL(" WHERE kind='component_height'")
            count=0
            buffer=[]
            with c.cursor(name='facts_'+uuid4().hex) as cursor:
                cursor.execute(statement)
                for (value,) in cursor:
                    if dump:dump.write(json.dumps(value,sort_keys=True)+'\n')
                    payload=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
                    buffer.append((hashlib.sha256(payload).digest(),))
                    count+=1
                    if len(buffer)==8192:
                        store.executemany('INSERT INTO counts VALUES(?,1) ON CONFLICT(digest) DO UPDATE SET n=n+1',buffer);buffer=[]
                if buffer:store.executemany('INSERT INTO counts VALUES(?,1) ON CONFLICT(digest) DO UPDATE SET n=n+1',buffer)
            checksum=hashlib.sha256()
            for digest,n in store.execute('SELECT digest,n FROM counts ORDER BY digest'):
                checksum.update(digest);checksum.update(n.to_bytes(8,'big'))
            result[name]=dict(rows=count,multiset_sha256=checksum.hexdigest())
            if dump:dump.close()
            store.execute('DELETE FROM counts');store.commit()
    cache.unlink()
    return result


def run(args):
    if args.code_root:
        sys.path.insert(0,str(Path(args.code_root)/'src'))
        sys.path.insert(1,str(Path(args.code_root)))
    from sweden_ubem.database import initialize_database
    from sweden_ubem.ingest import initialize_sources
    import sweden_ubem.ingest as ingestion
    admin=os.environ['UBEM_TEST_ADMIN_URL']
    info=conninfo_to_dict(admin);info['dbname']='postgres';admin=make_conninfo(**info)
    name='ubem_test_loader_'+args.implementation+'_'+uuid4().hex
    with psycopg.connect(admin,autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    info['dbname']=name;url=make_conninfo(**info)
    result=dict(implementation=args.implementation,sample=str(args.sample),database=name,stages={},cache_state='uncontrolled; paired run order recorded by orchestrator')
    try:
        sources=SampleSources(args.sample)
        start=time.monotonic();initialize_database(url)
        result['schema_seconds']=time.monotonic()-start
        with psycopg.connect(url) as c:before=metrics(c)
        started=time.monotonic()
        for layer in ('geometry','register','addresses','properties','heights','epc'):
            stage=time.monotonic()
            profiler=None
            if args.profile_heights and layer=='heights':
                import cProfile
                profiler=cProfile.Profile();profiler.enable()
            if layer=='geometry' and args.interrupt_after:
                original=ingestion.bulk_insert
                seen=0
                failed=False
                def interrupt(c,table,records,**kwargs):
                    nonlocal seen,failed
                    if table=='raw_lm.record' and records and records[0]['kind']=='geometry':
                        if seen>=args.interrupt_after and not failed:
                            failed=True
                            raise RuntimeError('benchmark recovery interruption')
                        seen+=len(records)
                    return original(c,table,records,**kwargs)
                ingestion.bulk_insert=interrupt
                try:
                    initialize_sources(url,sources,layers=[layer])
                except RuntimeError as error:
                    if str(error)!='benchmark recovery interruption':raise
                    result['recovery_committed_original_rows']=seen
                finally:
                    ingestion.bulk_insert=original
                recovery=time.monotonic()
                initialize_sources(url,sources,layers=[layer])
                result['interrupted_resume_seconds']=time.monotonic()-recovery
            else:
                initialize_sources(url,sources,layers=[layer])
            result['stages'][layer]=dict(seconds=time.monotonic()-stage)
            if profiler:
                profiler.disable();profiler.dump_stats(str(args.output)+'.heights.prof')
            print(json.dumps(dict(implementation=args.implementation,layer=layer,**result['stages'][layer])),flush=True)
        if args.implementation=='new':
            from sweden_ubem.database import install_processing_indexes
            stage=time.monotonic();install_processing_indexes(url)
            result['index_build_seconds']=time.monotonic()-stage
        result['loader_seconds']=time.monotonic()-started
        stage=time.monotonic()
        for layer in ('geometry','register','addresses','properties','heights','epc'):
            initialize_sources(url,sources,layers=[layer])
        result['completed_resume_seconds']=time.monotonic()-stage
        result['peak_worker_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
        with psycopg.connect(url) as c:
            after=metrics(c)
            result['wal_bytes']=int(c.execute('SELECT pg_wal_lsn_diff(%s,%s)',(after['wal_lsn'],before['wal_lsn'])).fetchone()[0])
            result['source_rows']=c.execute("SELECT stage,COALESCE(sum((checkpoint->>'source_rows')::bigint) FILTER(WHERE checkpoint->>'file_complete' IS DISTINCT FROM 'true'),0)::bigint FROM catalog.work_unit WHERE state='complete' GROUP BY stage ORDER BY stage").fetchall()
        result['storage']=after
        result['temporary_bytes']=after['temporary_bytes']-before['temporary_bytes']
        result['facts']=signatures(url,args.output,args.dump_facts)
        result['finished']=True
        Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    finally:
        with psycopg.connect(admin,autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--implementation',choices=['old','new'],required=True)
    parser.add_argument('--code-root',type=Path)
    parser.add_argument('--sample',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--interrupt-after',type=int,default=0)
    parser.add_argument('--dump-facts',action='store_true')
    parser.add_argument('--profile-heights',action='store_true')
    args=parser.parse_args()
    print(json.dumps(run(args)),flush=True)
