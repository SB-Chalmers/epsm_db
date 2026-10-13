"""Replace only the empty, explicitly identified 0.0.4 development prototype."""
import json
from pathlib import Path
import psycopg
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from psycopg import sql
from sweden_ubem.database import initialize_database
from sweden_ubem.asset_operations import publish_json

ROOT=Path(__file__).resolve().parents[1]
NAME='sweden_ubem_v004'
URL='postgresql://ubem_owner:ubem-local-only@127.0.0.1:55433/'+NAME


def reset():
    expected=(ROOT/'output/runtime/native-pg17-v004/data').resolve()
    with psycopg.connect(URL,options='-c default_transaction_read_only=on') as c:
        name,port,data=c.execute("SELECT current_database(),current_setting('port')::int,current_setting('data_directory')").fetchone()
        if (name,port,Path(data).resolve())!=(NAME,55433,expected):raise ValueError('Exact prototype target check failed')
        if c.execute('SELECT version FROM catalog.schema_baseline').fetchall()!=[('0.0.4',)]:raise ValueError('Unexpected existing baseline')
        counts={}
        for schema,table in c.execute("SELECT n.nspname,r.relname FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE r.relkind='r' AND n.nspname IN ('catalog','stock','evidence','context','scope','quality','raw_lm','raw_epc','raw_height','raw_scb')").fetchall():
            if (schema,table) in [('catalog','schema_baseline'),('catalog','schema_migration'),('catalog','field_mapping')]:continue
            count=c.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table))).fetchone()[0]
            counts[schema+'.'+table]=count
        if any(counts.values()):raise ValueError('Prototype contains application data; refusing removal')
    info=conninfo_to_dict(URL);info['dbname']='postgres';admin=make_conninfo(**info)
    with psycopg.connect(admin,autocommit=True) as c:
        if c.execute('SELECT count(*) FROM pg_stat_activity WHERE datname=%s',(NAME,)).fetchone()[0]:raise ValueError('Prototype has active clients')
        if c.execute("SELECT current_setting('port')::int,current_setting('data_directory')").fetchone()!=(55433,str(expected)):raise ValueError('Server target changed')
        publish_json(ROOT/'output/restart-2026-10-13/empty-prototype-replacement.json',dict(database=NAME,port=55433,data_directory=str(expected),empty_table_counts=counts))
        c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(NAME)))
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(NAME)))
    return initialize_database(URL)

if __name__=='__main__':print(json.dumps(reset()))
