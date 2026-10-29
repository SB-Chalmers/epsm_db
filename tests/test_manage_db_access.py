"""Native PostgreSQL enrollment checks. Integration uses an owned disposable DB only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
import pytest

from scripts import manage_db_access as admin
from scripts import db_access_common as common
from scripts import postgres_enrollment as web


def configuration():
    return dict(mode=admin.MODE,container='test-container',database='test_database',
                database_host='database.example.org',database_port=5432,
                activation_origin='https://access.example.org',sender='ssanjay@chalmers.se',
                admin_email='ssanjay@chalmers.se',service_database_url='configured-separately')


@pytest.mark.parametrize('value',['alice@other.se','../alice','alice;DROP ROLE x','ssanjay\nBcc:x@evil.se','a'*32])
def test_identity_injection_rejected(value):
    with pytest.raises(ValueError):
        common.cid(value)


def test_cid_and_endpoint_validation():
    assert common.cid(' SSANJAY@chalmers.se ')=='ssanjay'
    with pytest.raises(ValueError):
        common.address('ssanjay@chalmers.se\nBcc:other@example.org')
    for value in ['http://example.org','https://user:pass@example.org','https://example.org/path']:
        with pytest.raises(ValueError):
            common.https_origin(value)
    assert common.https_origin('https://access.example.org/')=='https://access.example.org'


def test_invitation_uses_fragment_and_no_temporary_password():
    token=secrets.token_urlsafe(32)
    message=common.email_message(configuration(),'ssanjay',token)
    body=message.get_content()
    assert message['To']=='ssanjay@chalmers.se'
    assert '/#cid=ssanjay&token='+token in body
    assert 'ssh-keygen' not in body
    assert 'Password:' not in body
    ready=common.email_message(configuration(),'ssanjay').get_content()
    assert 'Username: ssanjay' in ready and 'QGIS' in ready and 'psycopg.connect' in ready
    assert token not in ready


def test_generated_role_has_no_login_or_password_and_readonly_grants():
    query=admin.new_invitation_sql(configuration(),'ssanjay',secrets.token_urlsafe(32))
    assert 'NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE' in query
    assert 'GRANT ubem_readonly TO "ssanjay"' in query
    assert 'GRANT SELECT ON catalog.dataset,catalog.dataset_layer' in query
    assert 'ssh' not in query and 'PASSWORD ' not in query


def test_registration_fails_closed_before_provisioning_when_endpoints_unready(monkeypatch):
    monkeypatch.setattr(admin,'read_config',configuration)
    def not_ready(config):
        raise ValueError('not ready')
    monkeypatch.setattr(admin,'readiness',not_ready)
    monkeypatch.setattr(admin,'sql',lambda *a:pytest.fail('Must not provision account'))
    with pytest.raises(ValueError,match='not ready'):
        admin.register(SimpleNamespace(cid='ssanjay'))


def test_email_queue_failure_keeps_private_draft(tmp_path,monkeypatch):
    def fail(*args,**kwargs):
        raise subprocess.CalledProcessError(1,args[0])
    monkeypatch.setattr(common.subprocess,'run',fail)
    queued,path=common.deliver(configuration(),common.email_message(configuration(),'ssanjay'),tmp_path)
    assert not queued and Path(path).exists()
    assert Path(path).stat().st_mode&0o777==0o600


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web,'deliver',lambda *a,**k:(True,'private-queued-file'))
    return web.create_app(configuration()).test_client()


def request_body():
    return dict(cid='ssanjay',token=secrets.token_urlsafe(32),password='A long test password!',confirm='A long test password!')


def test_activation_validates_origin_json_and_password_before_database(client,monkeypatch):
    monkeypatch.setattr(web,'activate',lambda *a:pytest.fail('Must not connect'))
    base='https://access.example.org'
    assert client.post('/activate',base_url=base,json=request_body(),headers={'Origin':'https://evil.example.org'}).status_code==403
    assert client.post('/activate',base_url=base,data='bad').status_code==415
    body=request_body();body['confirm']='wrong'
    assert client.post('/activate',base_url=base,json=body).status_code==400
    body=request_body();body['token']='not-a-token'
    assert client.post('/activate',base_url=base,json=body).status_code==400
    body=request_body();body['password']=body['confirm']='short'
    assert client.post('/activate',base_url=base,json=body).status_code==400


def test_activation_returns_connection_details_without_password(client,monkeypatch):
    monkeypatch.setattr(web,'activate',lambda config,name,*a:common.connection_details(config,name))
    result=client.post('/activate',base_url='https://access.example.org',json=request_body())
    assert result.status_code==200
    assert result.json['connection']['username']=='ssanjay'
    assert 'password' not in result.json['connection']
    assert 'A long test password!' not in result.get_data(as_text=True)
    assert result.headers['Cache-Control']=='no-store'


def test_mail_failure_does_not_lose_successful_activation(client,monkeypatch):
    monkeypatch.setattr(web,'activate',lambda config,name,*a:common.connection_details(config,name))
    monkeypatch.setattr(web,'deliver',lambda *a,**k:(False,'private-draft'))
    response=client.post('/activate',base_url='https://access.example.org',json=request_body())
    assert response.status_code==200
    assert 'could not be queued' in response.json['message']


def test_page_excludes_tokens_from_request_path_and_sets_browser_protections(client):
    response=client.get('/',base_url='https://access.example.org')
    assert response.status_code==200
    assert 'frame-ancestors' in response.headers['Content-Security-Policy']
    assert response.headers['Referrer-Policy']=='no-referrer'
    javascript=client.get('/setup.js',base_url='https://access.example.org').get_data(as_text=True)
    assert 'location.hash' in javascript and 'history.replaceState' in javascript
    assert client.get('/',base_url='https://untrusted.example.org').status_code==400


def test_postfix_probe_verifies_tls(monkeypatch):
    from scripts import setup_postfix_relay as mail
    events=[]
    class SMTP:
        esmtp_features={}
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def ehlo(self): events.append('ehlo');return 250,b'OK'
        def has_extn(self,name): return True
        def starttls(self,context):
            assert context.check_hostname
            events.append('tls');self.esmtp_features={'auth':'LOGIN NTLM'}
    monkeypatch.setattr(mail.smtplib,'SMTP',SMTP)
    assert mail.probe_relay('smtp.chalmers.se',587,'db.localdomain')=={'LOGIN','NTLM'}
    assert events==['ehlo','tls','ehlo']


@pytest.fixture(scope='module')
def database():
    dsn=os.environ.get('UBEM_ONBOARDING_TEST_ADMIN_URL')
    if not dsn:
        pytest.skip('Set UBEM_ONBOARDING_TEST_ADMIN_URL for owned disposable PostgreSQL checks')
    owner=psycopg.connect(dsn,autocommit=True)
    suffix=secrets.token_hex(6)
    database='ubem_enrollment_test_'+suffix
    service='ubemenroll'+suffix
    service_password=secrets.token_urlsafe(32)
    roles=[service]
    created=False
    try:
        owner.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(service),sql.Literal(admin.scram_generated_password(service_password))))
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
        created=True
        db_dsn=make_conninfo(dsn,dbname=database)
        with psycopg.connect(db_dsn,autocommit=True) as connection:
            connection.execute('CREATE SCHEMA catalog; CREATE SCHEMA mart; REVOKE ALL ON SCHEMA catalog,mart FROM PUBLIC; CREATE TABLE catalog.dataset(dataset_id text); CREATE TABLE catalog.dataset_layer(dataset_id text); INSERT INTO catalog.dataset VALUES (\'uddevalla\'); CREATE VIEW mart.datasets AS SELECT * FROM catalog.dataset; GRANT USAGE ON SCHEMA mart TO ubem_readonly; GRANT SELECT ON mart.datasets TO ubem_readonly;')
            migration=(Path(__file__).resolve().parents[1]/'sql/access/enrollment.sql').read_text().replace('__SERVICE_ROLE__',service)
            connection.execute(migration)
        config=configuration()
        config['database']=database
        config['service_database_url']=make_conninfo(db_dsn,user=service,password=service_password)
        def invite():
            name='u'+secrets.token_hex(8)
            token=secrets.token_urlsafe(32)
            with psycopg.connect(db_dsn,autocommit=True) as connection:
                connection.execute(admin.new_invitation_sql(config,name,token))
            roles.append(name)
            return name,token
        yield SimpleNamespace(config=config,owner_dsn=db_dsn,service=service,invite=invite)
    finally:
        if created:
            owner.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database)))
        for name in reversed(roles):
            owner.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(name)))
        owner.close()


def test_real_activation_and_native_postgres_login(database,monkeypatch):
    monkeypatch.setattr(web,'deliver',lambda *a,**k:(True,'queued'))
    name,token=database.invite()
    password='Test-only long password åäö'
    client=web.create_app(database.config).test_client()
    body=dict(cid=name,token=token,password=password,confirm=password)
    response=client.post('/activate',base_url='https://access.example.org',json=body)
    assert response.status_code==200,response.json
    assert client.post('/activate',base_url='https://access.example.org',json=body).status_code==400
    with psycopg.connect(make_conninfo(database.owner_dsn,user=name,password=password),autocommit=True) as user:
        assert user.execute('SHOW default_transaction_read_only').fetchone()[0]=='on'
        assert user.execute('SELECT * FROM mart.datasets').fetchone()[0]=='uddevalla'
        assert user.execute('SELECT * FROM catalog.dataset').fetchone()[0]=='uddevalla'
        user.execute('SET default_transaction_read_only=off')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            user.execute("UPDATE catalog.dataset SET dataset_id='tampered'")
    with psycopg.connect(database.owner_dsn) as owner:
        status=owner.execute('SELECT status,token_hash FROM ubem_access.invitation WHERE cid=%s',(name,)).fetchone()
        assert status==('active',None)


def test_real_service_role_cannot_administer_accounts_or_read_invites(database):
    with psycopg.connect(database.config['service_database_url'],autocommit=True) as service:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            service.execute('SELECT * FROM ubem_access.invitation')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            service.execute('CREATE ROLE forbidden_new_role')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            service.execute('SELECT * FROM catalog.dataset')


def test_real_expired_and_wrong_invites_never_enable_login(database):
    name,token=database.invite()
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification):
        web.activate(database.config,name,secrets.token_urlsafe(32),'Strong enough password!')
    with psycopg.connect(database.owner_dsn,autocommit=True) as owner:
        owner.execute("UPDATE ubem_access.invitation SET expires_at=clock_timestamp()-interval '1 second' WHERE cid=%s",(name,))
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification):
        web.activate(database.config,name,token,'Strong enough password!')
    with psycopg.connect(database.owner_dsn) as owner:
        assert owner.execute('SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(name,)).fetchone()[0] is False


def test_real_reset_revokes_login_and_issues_new_one_use_invitation(database):
    name,token=database.invite()
    web.activate(database.config,name,token,'Original long password!')
    replacement=secrets.token_urlsafe(32)
    with psycopg.connect(database.owner_dsn,autocommit=True) as owner:
        owner.execute(admin.new_invitation_sql(database.config,name,replacement,reset=True))
        assert owner.execute('SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(name,)).fetchone()[0] is False
    web.activate(database.config,name,replacement,'Replacement password!')


def test_real_invitation_race_activates_only_once(database):
    from concurrent.futures import ThreadPoolExecutor
    name,token=database.invite()
    def attempt():
        try:
            web.activate(database.config,name,token,'One winner password!')
            return 'activated'
        except psycopg.errors.InvalidAuthorizationSpecification:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:attempt(),range(2)))
    assert sorted(results)==['activated','rejected']


def test_real_invitation_cannot_enable_role_with_extra_membership(database):
    name,token=database.invite()
    with psycopg.connect(database.owner_dsn,autocommit=True) as owner:
        owner.execute(sql.SQL('GRANT pg_write_server_files TO {}').format(sql.Identifier(name)))
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification):
        web.activate(database.config,name,token,'Strong enough password!')
    with psycopg.connect(database.owner_dsn) as owner:
        assert owner.execute('SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(name,)).fetchone()[0] is False


def tailscale_configuration():
    config=configuration()
    config.update(transport='tailscale',database_host='db.tail-test.ts.net',database_port=55433,
                  activation_origin='https://db.tail-test.ts.net',tailscale_dns='db.tail-test.ts.net',
                  tailscale_node_id='test-node-id',tailscale_share_url='https://login.tailscale.com/admin/invite/test-private-link')
    return config


def private_serve_state():
    return dict(TCP={'443':{'HTTPS':True},'55433':{'TCPForward':'127.0.0.1:55433'}},
                Web={'db.tail-test.ts.net:443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:18080'}}}})


def test_tailscale_email_includes_network_invitation_and_correct_ssl_setting():
    config=tailscale_configuration()
    assert common.connection_details(config,'ssanjay')['sslmode']=='disable'
    invitation=common.email_message(config,'ssanjay',secrets.token_urlsafe(32)).get_content()
    assert config['tailscale_share_url'] in invitation
    assert 'Install Tailscale' in invitation and 'Chalmers network/VPN' not in invitation
    ready=common.email_message(config,'ssanjay').get_content()
    assert "sslmode='disable'" in ready and 'Tailscale encrypts' in ready
    config['database_host']='unrelated.example.org'
    with pytest.raises(ValueError,match='identity'):
        common.connection_details(config,'ssanjay')


def test_tailscale_serve_rejects_public_funnel_and_existing_services():
    config=tailscale_configuration()
    admin.check_serve_config(config,{},require_ready=False)
    admin.check_serve_config(config,private_serve_state(),require_ready=True)
    state=private_serve_state();state['AllowFunnel']={'db.tail-test.ts.net:443':True}
    with pytest.raises(ValueError,match='Funnel'):
        admin.check_serve_config(config,state)
    state=private_serve_state();state['TCP']['55433']['TCPForward']='127.0.0.1:22'
    with pytest.raises(ValueError,match='different'):
        admin.check_serve_config(config,state)
    state=private_serve_state();state['Web']['db.tail-test.ts.net:443']['Handlers']['/']['Proxy']='http://127.0.0.1:9999'
    with pytest.raises(ValueError,match='different'):
        admin.check_serve_config(config,state)
    with pytest.raises(ValueError,match='not configured'):
        admin.check_serve_config(config,{},require_ready=True)


def test_tailscale_identity_requires_running_node_and_magicdns(monkeypatch):
    status=dict(BackendState='Running',Self=dict(Online=True,DNSName='db.tail-test.ts.net.',ID='test-node-id',TailscaleIPs=['100.70.80.90']))
    monkeypatch.setattr(admin,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(status)))
    assert admin.tailscale_identity()['dns']=='db.tail-test.ts.net'
    status['BackendState']='Stopped'
    with pytest.raises(ValueError,match='Connect'):
        admin.tailscale_identity()


def test_tailscale_readiness_rejects_changed_node_before_network_access(monkeypatch):
    monkeypatch.setattr(admin,'tailscale_identity',lambda:dict(node_id='another-node',dns='db.tail-test.ts.net'))
    monkeypatch.setattr(admin,'urlopen',lambda *a,**k:pytest.fail('Must not fetch or send credentials'))
    with pytest.raises(ValueError,match='identity'):
        admin.readiness(tailscale_configuration())


def test_tailscale_readiness_checks_https_and_sasl_not_database_tls(monkeypatch):
    import io
    config=tailscale_configuration()
    monkeypatch.setattr(admin,'tailscale_identity',lambda:dict(node_id=config['tailscale_node_id'],dns=config['tailscale_dns']))
    monkeypatch.setattr(admin,'serve_state',private_serve_state)
    monkeypatch.setattr(admin,'urlopen',lambda *a,**k:io.BytesIO(b'{"service":"ubem-postgres-enrollment","status":"ok"}'))
    checks=[]
    monkeypatch.setattr(admin,'check_postgres_auth',lambda c:checks.append(c['database_host']))
    monkeypatch.setattr(admin,'run',lambda *a,**k:pytest.fail('Must not require PostgreSQL TLS over Tailscale'))
    admin.readiness(config)
    assert checks==['db.tail-test.ts.net']


def test_tailscale_probe_rejects_trust_authentication(monkeypatch):
    import struct
    class Socket:
        def __init__(self,auth):
            self.data=b'R'+struct.pack('!II',8,auth)
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def sendall(self,data):
            assert b'password' not in data
        def recv(self,size):
            # Deliberate fragmented TCP reads.
            chunk=self.data[:1];self.data=self.data[1:];return chunk
    monkeypatch.setattr(admin.socket,'create_connection',lambda *a,**k:Socket(10))
    admin.check_postgres_auth(tailscale_configuration())
    monkeypatch.setattr(admin.socket,'create_connection',lambda *a,**k:Socket(0))
    with pytest.raises(ValueError,match='SCRAM'):
        admin.check_postgres_auth(tailscale_configuration())


def test_tailscale_page_has_correct_network_guidance():
    client=web.create_app(tailscale_configuration()).test_client()
    page=client.get('/',base_url='https://db.tail-test.ts.net')
    assert page.status_code==200
    assert 'Keep Tailscale connected' in page.get_data(as_text=True)


def test_first_registration_runs_guided_setup_automatically(tmp_path,monkeypatch):
    monkeypatch.setattr(admin,'STATE',tmp_path)
    monkeypatch.setattr(admin.os,'geteuid',lambda:0)
    monkeypatch.setattr(admin.sys,'argv',['manage_db_access.py','register','ssanjay'])
    steps=[]
    monkeypatch.setattr(admin,'setup',lambda args:steps.append('setup') or True)
    monkeypatch.setattr(admin,'register',lambda args,**k:steps.append(('register',args.cid)))
    admin.main()
    assert steps==['setup',('register','ssanjay')]


def test_cancelled_first_setup_never_registers_user(tmp_path,monkeypatch):
    monkeypatch.setattr(admin,'STATE',tmp_path)
    monkeypatch.setattr(admin.os,'geteuid',lambda:0)
    monkeypatch.setattr(admin.sys,'argv',['manage_db_access.py'])
    monkeypatch.setattr(admin,'setup',lambda args:False)
    monkeypatch.setattr(admin,'register',lambda *a,**k:pytest.fail('Cancelled setup must stop registration'))
    admin.main()


def test_missing_setup_error_contains_absolute_script_path(tmp_path,monkeypatch):
    monkeypatch.setattr(admin,'STATE',tmp_path)
    with pytest.raises(ValueError) as error:
        admin.read_config()
    assert str(Path(admin.__file__).resolve())+' setup' in str(error.value)


def test_setup_waits_for_first_certificate_then_succeeds(monkeypatch):
    ticks=[0.0]
    calls=[]
    monkeypatch.setattr(admin.time,'monotonic',lambda:ticks[0])
    monkeypatch.setattr(admin.time,'sleep',lambda seconds:ticks.__setitem__(0,ticks[0]+seconds))
    def ready(config):
        calls.append(config)
        if len(calls)<3:
            raise ValueError('TLS handshake timed out') from TimeoutError('certificate pending')
    monkeypatch.setattr(admin,'readiness',ready)
    admin.wait_readiness(tailscale_configuration(),timeout=10,interval=2)
    assert len(calls)==3 and ticks[0]==4


def test_setup_does_not_retry_unsafe_configuration(monkeypatch):
    def unsafe(config):
        raise ValueError('not ready') from ValueError('Funnel is enabled')
    monkeypatch.setattr(admin,'readiness',unsafe)
    monkeypatch.setattr(admin.time,'sleep',lambda *a:pytest.fail('Unsafe settings must fail immediately'))
    with pytest.raises(ValueError,match='not ready'):
        admin.wait_readiness(tailscale_configuration())


def test_certificate_wait_timeout_gives_check_command_not_reinstall(monkeypatch):
    ticks=[0.0]
    monkeypatch.setattr(admin.time,'monotonic',lambda:ticks[0])
    monkeypatch.setattr(admin.time,'sleep',lambda seconds:ticks.__setitem__(0,ticks[0]+seconds))
    def pending(config):
        raise ValueError('TLS handshake timed out') from TimeoutError('certificate pending')
    monkeypatch.setattr(admin,'readiness',pending)
    with pytest.raises(ValueError,match='Services are installed') as error:
        admin.wait_readiness(tailscale_configuration(),timeout=4,interval=2)
    assert str(Path(admin.__file__).resolve())+' check' in str(error.value)
