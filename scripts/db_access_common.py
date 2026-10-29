"""Shared, stdlib-only native PostgreSQL onboarding helpers."""
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import subprocess
import tempfile
from urllib.parse import urlsplit, urlencode

ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/root','LANG':'C.UTF-8'}


def cid(value):
    value=value.strip().lower()
    if value.endswith('@chalmers.se'):
        value=value[:-len('@chalmers.se')]
    if not re.fullmatch(r'[a-z][a-z0-9]{1,30}',value):
        raise ValueError('Enter a CID such as ssanjay or ssanjay@chalmers.se.')
    return value


def host(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',value):
        raise ValueError('Enter a hostname or IPv4 address reachable over the configured private network.')
    return value


def identifier(value):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',value):
        raise ValueError('Use lowercase letters, digits and underscores for the database name.')
    return value


def address(value):
    if '\n' in value or '\r' in value or parseaddr(value)[1]!=value or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+',value):
        raise ValueError('Enter a plain email address.')
    return value


def https_origin(value):
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path not in ('','/')):
        raise ValueError('Enter the HTTPS origin of the password page, e.g. https://db-access.example.org.')
    host(parsed.hostname)
    if parsed.port is not None and not 1<=parsed.port<=65535:
        raise ValueError('Invalid HTTPS port.')
    return value.rstrip('/')


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def atomic(path,content,mode=0o600):
    path=Path(path)
    if path.is_symlink():
        raise ValueError(f'Refusing symlink: {path}')
    fd,temporary=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary,mode)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def connection_details(config,name):
    mode='verify-full'
    if config.get('transport')=='tailscale':
        if (not config.get('tailscale_node_id') or config['database_host']!=config.get('tailscale_dns')
            or not config['database_host'].endswith('.ts.net')):
            raise ValueError('Invalid Tailscale database identity.')
        mode='disable'
    return dict(host=config['database_host'],port=config['database_port'],
                database=config['database'],username=cid(name),sslmode=mode)


def network_instructions(config):
    if config.get('transport')!='tailscale':
        return 'Connect to the Chalmers network/VPN.'
    text='Install Tailscale from https://tailscale.com/download and sign in to your own account.\n'
    if config.get('tailscale_share_url'):
        text+='Accept the invitation to this database laptop:\n'+config['tailscale_share_url']+'\n'
    else:
        text+='The administrator must first share this database laptop with your Tailscale account.\n'
    text+='Keep Tailscale connected while setting your password and using QGIS/Python.'
    return text


def details_text(config,name):
    details=connection_details(config,name)
    network=('Tailscale connected and access to the database laptop required' if config.get('transport')=='tailscale' else 'Chalmers network/VPN required')
    security=('PostgreSQL SSL mode: Disable. Tailscale encrypts and authenticates the network connection.\n'
              'Use these settings only through the configured Tailscale hostname.'
              if config.get('transport')=='tailscale' else
              'PostgreSQL SSL mode: verify-full. Configure the trusted root CA if needed.')
    return f'''PostgreSQL connection details ({network}):
Host: {details['host']}
Port: {details['port']}
Database: {details['database']}
Username: {details['username']}
Password: the password you chose on the setup page
{security}

QGIS: Add PostgreSQL connection; enter these fields and your chosen password.
Use the PostgreSQL SSL mode given above. Keep Tailscale connected for Tailscale access.

Python (install psycopg[binary] in your own environment):
    import getpass
    import psycopg
    connection = psycopg.connect(
        host={details['host']!r}, port={details['port']},
        dbname={details['database']!r}, user={details['username']!r},
        password=getpass.getpass("Database password: "), sslmode={details['sslmode']!r})

The epsm_db client also accepts this PostgreSQL connection string through
UBEM_DATABASE_URL or Database(database_url=...).
Access is read-only across the granted mart datasets, not restricted to one municipality.
Contact {config['admin_email']} for support or a new password setup invitation.
'''


def email_message(config,name,token=None):
    name=cid(name)
    message=EmailMessage()
    message['From']=config['sender']
    message['To']=name+'@chalmers.se'
    message['Reply-To']=config['admin_email']
    message['Date']=format_datetime(datetime.now(timezone.utc))
    message['Message-ID']=make_msgid(domain=config['sender'].split('@')[1])
    if token is not None:
        link=config['activation_origin']+'/#'+urlencode({'cid':name,'token':token})
        message['Subject']='Choose your Chalmers UBEM database password'
        body=f'''Your PostgreSQL account {name} has been registered.

{network_instructions(config)}

Then open this one-time password setup link within 24 hours:
{link}

Choose your own database password there. Your account stays disabled until you do.
No Linux login, SSH key, or temporary password is required.
After activation you will see, and receive by email, the connection details for
QGIS and Python. Your chosen password is never emailed.
If you did not request access, ignore this invitation and contact {config['admin_email']}.
'''
    else:
        message['Subject']='Your Chalmers UBEM PostgreSQL account is ready'
        body=details_text(config,name)
    message.set_content(body)
    return message


def deliver(config,message,outbox,local_smtp=False):
    """Always save a private copy; distinguish queued mail from confirmed delivery."""
    path=Path(outbox)
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    draft=path/(stamp+'-'+message['To'].split('@')[0]+'.eml')
    atomic(draft,message.as_string())
    try:
        if local_smtp:
            with smtplib.SMTP('127.0.0.1',25,timeout=10) as smtp:
                smtp.send_message(message,from_addr=config['sender'],to_addrs=[message['To']])
        else:
            subprocess.run(['/usr/sbin/sendmail','-i','-f',config['sender'],'--',message['To']],
                           input=message.as_bytes(),capture_output=True,check=True,timeout=30,env=ENV)
    except (OSError,subprocess.SubprocessError,smtplib.SMTPException):
        return False,str(draft)
    draft.rename(draft.with_suffix('.queued.eml'))
    return True,str(draft.with_suffix('.queued.eml'))


def prompt(label,default=None):
    value=input(label+(f' [{default}]' if default else '')+': ').strip()
    return value or default or ''
