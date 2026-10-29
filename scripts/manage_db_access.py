#!/usr/bin/env python3
"""Native PostgreSQL user onboarding: configure once, then enter a Chalmers CID."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import fcntl
import hashlib
import getpass
import hmac
import json
import os
from pathlib import Path
import pwd
import secrets
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import quote
from urllib.request import urlopen

try:
    from .db_access_common import (ENV,cid,host,identifier,address,https_origin,prompt,
                                  atomic,token_hash,email_message,deliver)
except ImportError:
    from db_access_common import (ENV,cid,host,identifier,address,https_origin,prompt,
                                 atomic,token_hash,email_message,deliver)

STATE=Path('/etc/ubem-access')
INSTALL=Path('/opt/ubem-enrollment')
APP_USER='ubem-enrollment'
APP_ROLE='ubem_enrollment'
MODE='postgres-native-v1'


def run(args,**kwargs):
    return subprocess.run([str(a) for a in args],env=ENV,check=True,**kwargs)


def sql(config,query):
    result=run(['/usr/bin/docker','exec','-i',config['container'],'psql','-X',
                '-v','ON_ERROR_STOP=1','-U','ubem_owner','-d',config['database'],'-At'],
               input=query,text=True,capture_output=True)
    return result.stdout.strip()


def literal(value):
    # All SQL identifiers are separately validated. Standard-conforming string literals.
    return "'"+str(value).replace("'","''")+"'"


def scram_generated_password(password):
    """Only for our ASCII random service secret; libpq handles end-user passwords."""
    salt=secrets.token_bytes(24)
    salted=hashlib.pbkdf2_hmac('sha256',password.encode('ascii'),salt,32768)
    stored=hashlib.sha256(hmac.digest(salted,b'Client Key','sha256')).digest()
    server=hmac.digest(salted,b'Server Key','sha256')
    b64=lambda v:base64.b64encode(v).decode()
    return f'SCRAM-SHA-256$32768:{b64(salt)}${b64(stored)}:{b64(server)}'


def read_config():
    path=STATE/'admin.json'
    if not path.exists():
        raise ValueError('First run: sudo python3 '+str(Path(__file__).resolve())+' setup')
    config=json.loads(path.read_text())
    if config.get('mode')!=MODE:
        raise ValueError('Account configuration is not for native PostgreSQL onboarding.')
    return config


def directory(path,mode=0o700):
    if path.is_symlink():
        raise ValueError(f'Refusing symlink: {path}')
    path.mkdir(parents=True,exist_ok=True,mode=mode)
    os.chmod(path,mode)


def tailscale_identity():
    status=json.loads(run(['/usr/bin/tailscale','status','--json'],capture_output=True,text=True).stdout)
    node=status.get('Self') or {}
    name=node.get('DNSName','').rstrip('.')
    if status.get('BackendState')!='Running' or not node.get('Online') or not name.endswith('.ts.net'):
        raise ValueError('Connect this laptop to Tailscale and enable MagicDNS, then retry setup.')
    return dict(dns=host(name),node_id=node['ID'],ips=node.get('TailscaleIPs',[]))


def serve_state():
    return json.loads(run(['/usr/bin/tailscale','serve','status','--json'],capture_output=True,text=True).stdout)


def check_serve_config(config,state,require_ready=False):
    tcp=state.get('TCP') or {}
    web=state.get('Web') or {}
    port=str(config['database_port'])
    expected='127.0.0.1:55433'
    forwarding=tcp.get(port)
    if forwarding and forwarding.get('TCPForward')!=expected:
        raise ValueError('A different Tailscale service already uses the database port. No service was overwritten.')
    if tcp.get('443') and not tcp['443'].get('HTTPS'):
        raise ValueError('Tailscale port 443 already serves a different protocol.')
    handlers=(web.get(config['tailscale_dns']+':443') or {}).get('Handlers') or {}
    existing=handlers.get('/')
    if existing and existing.get('Proxy')!='http://127.0.0.1:18080':
        raise ValueError('A different Tailscale HTTPS service already uses /. No service was overwritten.')
    if any(enabled and (key.endswith(':443') or key.endswith(':'+port))
           for key,enabled in (state.get('AllowFunnel') or {}).items()):
        raise ValueError('Funnel is enabled on an onboarding port. Disable that public route before private setup.')
    if require_ready and (not forwarding or not existing or not (tcp.get('443') or {}).get('HTTPS')):
        raise ValueError('Private Tailscale HTTPS/database forwarding is not configured. Rerun setup.')


def enable_tailscale(config):
    before=serve_state()
    check_serve_config(config,before)
    atomic(STATE/('tailscale-serve-before-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json'),
           json.dumps(before,indent=2)+'\n')
    print('Publishing the private password page with Tailscale Serve.')
    print('If prompted, open the Tailscale settings URL and enable HTTPS certificates for your tailnet.')
    run(['/usr/bin/tailscale','serve','--bg','--yes','--https=443','http://127.0.0.1:18080'])
    run(['/usr/bin/tailscale','serve','--bg','--yes',f"--tcp={config['database_port']}",'tcp://127.0.0.1:55433'])
    check_serve_config(config,serve_state(),require_ready=True)


def read_exact(connection,size):
    data=b''
    while len(data)<size:
        chunk=connection.recv(size-len(data))
        if not chunk:
            raise ValueError('Database closed the connection during its authentication greeting.')
        data+=chunk
    return data


def check_postgres_auth(config):
    # A startup greeting without credentials verifies that remote clients must
    # authenticate. In particular, do not publish Docker's local trust socket.
    payload=struct.pack('!I',196608)+b'user\x00ubemreadinessprobe\x00database\x00'+config['database'].encode()+b'\x00\x00'
    with socket.create_connection((config['database_host'],config['database_port']),timeout=5) as connection:
        connection.sendall(struct.pack('!I',len(payload)+4)+payload)
        kind=read_exact(connection,1)
        length=struct.unpack('!I',read_exact(connection,4))[0]
        if not 4<=length<=65536:
            raise ValueError('Invalid PostgreSQL authentication greeting.')
        body=read_exact(connection,length-4)
        if kind!=b'R' or len(body)<4 or struct.unpack('!I',body[:4])[0]!=10:
            raise ValueError('PostgreSQL did not require SCRAM/SASL authentication through the Tailscale port.')


def readiness(config):
    try:
        if config.get('transport')=='tailscale':
            identity=tailscale_identity()
            if (identity['node_id']!=config['tailscale_node_id'] or identity['dns']!=config['database_host']
                or config['tailscale_dns']!=identity['dns'] or config['activation_origin']!='https://'+identity['dns']):
                raise ValueError('Tailscale identity/address changed; rerun setup.')
            check_serve_config(config,serve_state(),require_ready=True)
            if config.get('tailscale_share_expires') and datetime.fromisoformat(config['tailscale_share_expires'])<datetime.now(timezone.utc)+timedelta(hours=24):
                raise ValueError('Refresh the reusable Tailscale share link with setup before sending more invitations.')
        with urlopen(config['activation_origin']+'/health',timeout=10) as response:
            health=json.load(response)
        if health!={'service':'ubem-postgres-enrollment','status':'ok'}:
            raise ValueError('The HTTPS hostname does not serve the enrollment application.')
        if config.get('transport')=='tailscale':
            check_postgres_auth(config)
        else:
            run(['/usr/bin/openssl','s_client','-starttls','postgres','-connect',
                 f"{config['database_host']}:{config['database_port']}",'-servername',config['database_host'],
                 '-verify_hostname',config['database_host'],'-verify_return_error','-brief'],
                input='',text=True,capture_output=True,timeout=10)
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        if config.get('transport')=='tailscale':
            raise ValueError('Enrollment is not ready: '+str(error)) from error
        raise ValueError('Enrollment is not ready: verify HTTPS enrollment and PostgreSQL TLS endpoints.') from error


def wait_readiness(config,timeout=120,interval=3):
    """Allow first-time certificate issuance; fail immediately on unsafe configuration."""
    started=time.monotonic()
    deadline=started+timeout
    while True:
        try:
            readiness(config)
            return
        except ValueError as error:
            if not isinstance(error.__cause__,(OSError,subprocess.TimeoutExpired)):
                raise
            remaining=deadline-time.monotonic()
            if remaining<=0:
                command='sudo python3 '+str(Path(__file__).resolve())+' check'
                raise ValueError('Services are installed, but initial network/certificate readiness is still pending. '
                                 'Wait briefly, then run: '+command) from error
            elapsed=int(time.monotonic()-started)
            print(f'Waiting for the Tailscale HTTPS certificate/services ({elapsed}s elapsed)...',flush=True)
            time.sleep(min(interval,remaining))


def setup(args):
    if (STATE/'infrastructure.json').exists():
        raise ValueError('The older SSH-account prototype was installed. Review/migrate it before native DB setup.')
    previous=read_config() if (STATE/'admin.json').exists() else {}
    identity=tailscale_identity()
    print('Tailscale detected. The database and password-page addresses are filled in automatically.')
    print('For new users, create a fresh REUSABLE device-share link in the Tailscale Machines page.')
    print('Paste that private link below to include it automatically in invitation emails.')
    print('Leave it blank only if every recipient already has Tailscale access to this laptop.')
    entered_share=getpass.getpass('Reusable Tailscale device-share link (hidden; Enter keeps existing, none clears): ').strip()
    share_url=('' if entered_share=='none' else entered_share or previous.get('tailscale_share_url',''))
    if share_url:
        from urllib.parse import urlsplit
        parsed=urlsplit(share_url)
        if (parsed.scheme!='https' or parsed.hostname not in ('login.tailscale.com','console.tailscale.com')
            or parsed.username or parsed.password or not parsed.path.strip('/')):
            raise ValueError('Paste an HTTPS Tailscale device-share link from the admin console.')
    same_share=share_url and share_url==previous.get('tailscale_share_url')
    share_expires=(previous.get('tailscale_share_expires') if same_share else
                   (datetime.now(timezone.utc)+timedelta(days=29)).isoformat() if share_url else None)
    config=dict(mode=MODE,transport='tailscale',container='ubem-national-postgis',
                database=identifier(prompt('Database',previous.get('database','epsm_lake'))),
                database_host=identity['dns'],database_port=55433,tailscale_dns=identity['dns'],
                tailscale_node_id=identity['node_id'],tailscale_ips=identity['ips'],
                activation_origin='https://'+identity['dns'],
                tailscale_share_url=share_url,tailscale_share_expires=share_expires,
                sender=address(prompt('From email address',previous.get('sender'))),
                admin_email=address(prompt('Administrator email',previous.get('admin_email'))))
    check_serve_config(config,serve_state())
    if sql(config,'SELECT version FROM catalog.schema_baseline;')!='0.0.4':
        raise ValueError('Database schema 0.0.4 is required.')
    role_exists=sql(config,f'SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname={literal(APP_ROLE)});')=='t'
    if role_exists and not previous:
        raise ValueError('The enrollment service role already exists without managed configuration. Refusing takeover.')
    if role_exists and sql(config,"SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) FROM pg_roles WHERE rolname='ubem_enrollment';")!='t':
        raise ValueError('Enrollment service role has unexpected privileges; administrator review required.')
    try:
        account=pwd.getpwnam(APP_USER)
    except KeyError:
        account=None
    if account and not previous:
        raise ValueError('The service account already exists without managed configuration. Refusing takeover.')
    print('\nThis installs a localhost-only enrollment service and its restricted database role.')
    print('Private Tailscale HTTPS: '+config['activation_origin'])
    print('Private PostgreSQL: '+config['database_host']+':55433')
    print('Database traffic is encrypted by Tailscale; PostgreSQL SSL is disabled in client settings.')
    print('Users need this laptop shared with them, with ports 443 and 55433 allowed.')
    if prompt('Type setup to install')!='setup':
        print('Cancelled.')
        return False
    # Persist installer intent so interrupted setup can be safely resumed.
    atomic(STATE/'admin.json',json.dumps(config,indent=2)+'\n')
    if account is None:
        run(['/usr/sbin/useradd','--system','--user-group','--create-home',
             '--home-dir','/var/lib/ubem-enrollment','--shell','/usr/sbin/nologin',APP_USER])
        account=pwd.getpwnam(APP_USER)
    directory(STATE,0o750)
    os.chown(STATE,0,account.pw_gid)
    directory(STATE/'outbox')
    directory(INSTALL,0o755)
    here=Path(__file__).resolve().parent
    for source,target in [('postgres_enrollment.py','app.py'),('db_access_common.py','db_access_common.py')]:
        atomic(INSTALL/target,(here/source).read_text(),0o644)
    if not (INSTALL/'venv/bin/python').exists():
        run(['/usr/bin/python3','-m','venv',INSTALL/'venv'])
    run([INSTALL/'venv/bin/pip','install','flask>=3.1,<4','gunicorn>=23,<24','psycopg[binary]>=3.2,<4'])
    secret=secrets.token_urlsafe(32)
    verifier=scram_generated_password(secret)
    role_statement=(f'ALTER ROLE {APP_ROLE} LOGIN PASSWORD {literal(verifier)};' if role_exists else
                    f'CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5 PASSWORD {literal(verifier)};')
    migration=(here.parent/'sql/access/enrollment.sql').read_text().replace('__SERVICE_ROLE__',APP_ROLE)
    mapping=(here.parent/'sql/access/qgis.sql').read_text()
    sql(config,'BEGIN;'+role_statement+f'GRANT CONNECT ON DATABASE "{config["database"]}" TO {APP_ROLE};'+migration+mapping+'COMMIT;')
    service=dict(config,service_database_url=f'postgresql://{APP_ROLE}:{quote(secret,safe="")}@127.0.0.1:55433/{config["database"]}',
                 outbox='/var/lib/ubem-enrollment/outbox')
    atomic(STATE/'service.json',json.dumps(service,indent=2)+'\n',0o640)
    os.chown(STATE/'service.json',0,account.pw_gid)
    atomic(STATE/'admin.json',json.dumps(config,indent=2)+'\n')
    unit=f'''[Unit]
Description=UBEM PostgreSQL password enrollment
After=network.target docker.service postfix.service
[Service]
User={APP_USER}
Group={APP_USER}
WorkingDirectory={INSTALL}
Environment=UBEM_ACCESS_CONFIG={STATE}/service.json
ExecStart={INSTALL}/venv/bin/gunicorn --bind 127.0.0.1:18080 --workers 2 --threads 2 --timeout 30 app:create_app()
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/ubem-enrollment
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UMask=0077
[Install]
WantedBy=multi-user.target
'''
    atomic(Path('/etc/systemd/system/ubem-enrollment.service'),unit,0o644)
    run(['/usr/bin/systemctl','daemon-reload'])
    run(['/usr/bin/systemctl','enable','--now','ubem-enrollment.service'])
    run(['/usr/bin/systemctl','restart','ubem-enrollment.service'])
    print('Enrollment backend installed on 127.0.0.1:18080.')
    enable_tailscale(config)
    wait_readiness(config)
    print('Tailscale onboarding checks passed. Next:')
    print('sudo python3 '+str(here/'manage_db_access.py'))
    return True


def install_mapping(config):
    migration=(Path(__file__).resolve().parent.parent/'sql/access/qgis.sql').read_text()
    sql(config,"BEGIN;SET LOCAL lock_timeout='5s';"+migration+'COMMIT;')
    print('QGIS mapping views installed: qgis.footprints and qgis.component_heights.')
    print('Refresh the PostgreSQL connection in QGIS, expand qgis, then drag a layer onto the map.')


def new_invitation_sql(config,name,token,reset=False):
    name=cid(name)
    grants=f'''GRANT CONNECT ON DATABASE "{config['database']}" TO "{name}";
GRANT ubem_readonly TO "{name}";
GRANT USAGE ON SCHEMA catalog TO "{name}";
GRANT SELECT ON catalog.dataset,catalog.dataset_layer TO "{name}";
ALTER ROLE "{name}" IN DATABASE "{config['database']}" SET default_transaction_read_only=on;
ALTER ROLE "{name}" SET statement_timeout='60s';
ALTER ROLE "{name}" SET idle_in_transaction_session_timeout='60s';'''
    if reset:
        # Only reset a role whose OID still matches our account record; never an arbitrary existing role.
        start=f'''DO $reset$ BEGIN
IF NOT EXISTS(SELECT 1 FROM ubem_access.invitation i JOIN pg_roles r ON r.oid=i.role_oid
WHERE i.cid={literal(name)} AND r.rolname=i.cid AND NOT r.rolsuper AND NOT r.rolcreaterole
AND NOT r.rolcreatedb AND NOT r.rolreplication AND NOT r.rolbypassrls) THEN
RAISE EXCEPTION 'Account needs administrator review'; END IF; END $reset$;
ALTER ROLE "{name}" NOLOGIN PASSWORD NULL;
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename={literal(name)};'''
        save=f'''UPDATE ubem_access.invitation SET status='pending',token_hash={literal(token_hash(token))},
expires_at=clock_timestamp()+interval '24 hours',activated_at=NULL WHERE cid={literal(name)};'''
    else:
        start=f'''CREATE ROLE "{name}" NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 20;'''
        save=f'''INSERT INTO ubem_access.invitation(cid,role_oid,token_hash,expires_at,status)
SELECT {literal(name)},oid,{literal(token_hash(token))},clock_timestamp()+interval '24 hours','pending'
FROM pg_roles WHERE rolname={literal(name)};'''
    return 'BEGIN;'+start+grants+save+'COMMIT;'


def register(args,reset=False):
    config=read_config()
    readiness(config)
    name=cid(args.cid or prompt('Chalmers CID'))
    if not reset and sql(config,f'SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname={literal(name)});')=='t':
        raise ValueError('This CID already has a database role. For a managed account, use reset '+name+'. No changes made.')
    if sql(config,"SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) FROM pg_roles WHERE rolname='ubem_readonly';")!='t':
        raise ValueError('Reader group has unexpected privileges; administrator review required.')
    if reset and prompt('Type the CID to disable existing sessions and send a new password link')!=name:
        print('Cancelled.')
        return
    token=secrets.token_urlsafe(32)
    sql(config,new_invitation_sql(config,name,token,reset=reset))
    queued,path=deliver(config,email_message(config,name,token),STATE/'outbox')
    print(f'{name}@chalmers.se: PostgreSQL account pending password setup.')
    print('Invitation accepted by Postfix queue; delivery not yet confirmed.' if queued else
          'Email NOT queued. Private draft: '+path)
    print('The user chooses a password and then receives QGIS/Python connection details automatically.')


def disable(args):
    config=read_config()
    name=cid(args.cid or prompt('Chalmers CID'))
    if prompt('Type CID to disable database access and terminate sessions')!=name:
        print('Cancelled.')
        return
    # Verify managed identity, even if a cluster role was deleted and its name reused.
    statement=new_invitation_sql(config,name,secrets.token_urlsafe(32),reset=True)
    statement=statement.replace('COMMIT;',f"UPDATE ubem_access.invitation SET status='disabled',token_hash=NULL WHERE cid={literal(name)};COMMIT;")
    sql(config,statement)
    print(name+' disabled. Existing database sessions terminated.')


@contextmanager
def locked_state():
    if not STATE.exists():
        directory(STATE)
    with (STATE/'admin.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command')
    commands.add_parser('setup',help='One-time installation and endpoint/email configuration.')
    commands.add_parser('check',help='Check private HTTPS enrollment and database authentication.')
    commands.add_parser('mapping',help='Install/update QGIS views with automatic geometry and feature IDs.')
    for name,description in [('register','Enter a CID and send a password setup invitation.'),
                             ('reset','Disable sessions and email a new password setup link.'),
                             ('disable','Disable a managed database account.')]:
        sub=commands.add_parser(name,help=description)
        sub.add_argument('cid',nargs='?')
    args=parser.parse_args()
    if os.geteuid()!=0:
        parser.error('Run with sudo on the database laptop.')
    if args.command is None:
        args.command='register'
        args.cid=None
    with locked_state():
        if args.command=='register' and not (STATE/'admin.json').exists():
            print('First run: starting the guided Tailscale setup before registration.')
            if not setup(args):
                return
        if args.command=='setup':
            setup(args)
        elif args.command=='check':
            readiness(read_config())
            print('Enrollment connectivity checks passed from this host. Also verify from a separate authorized Tailscale device.')
        elif args.command=='mapping':
            install_mapping(read_config())
        elif args.command=='disable':
            disable(args)
        else:
            register(args,reset=args.command=='reset')


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nCancelled.',file=sys.stderr)
        sys.exit(130)
    except (ValueError,KeyError,OSError,subprocess.SubprocessError) as error:
        print(f'Error: {error}',file=sys.stderr)
        sys.exit(1)
