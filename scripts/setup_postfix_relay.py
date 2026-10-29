#!/usr/bin/env python3
"""Configure this Ubuntu host's Postfix as an outbound-only authenticated relay client."""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone

try:
    from .db_access_common import ENV, atomic, host, prompt
except ImportError:  # Executed directly rather than imported as scripts.*
    from db_access_common import ENV, atomic, host, prompt


def run(args, **kwargs):
    return subprocess.run(args, env=ENV, check=True, **kwargs)


def probe_relay(relay, port, mail_hostname):
    """Verify the relay's TLS identity before asking for any credentials."""
    with smtplib.SMTP(relay, port, local_hostname=mail_hostname, timeout=10) as smtp:
        if smtp.ehlo()[0] != 250 or not smtp.has_extn('starttls'):
            raise ValueError('The relay did not offer STARTTLS. No credentials were requested.')
        smtp.starttls(context=ssl.create_default_context())
        if smtp.ehlo()[0] != 250:
            raise ValueError('The relay rejected EHLO after TLS.')
        return set(smtp.esmtp_features.get('auth', '').upper().split())


def main():
    if '--help' in sys.argv:
        print(__doc__)
        return
    if os.geteuid() != 0:
        raise ValueError('Run with sudo on the database laptop.')
    if len(sys.argv) != 1:
        raise ValueError('This script takes no command-line arguments.')
    print('Chalmers SMTP is the default. Its TLS identity is checked before password entry.')
    relay = host(prompt('Outgoing SMTP relay hostname', 'smtp.chalmers.se'))
    port = int(prompt('Relay port', '587'))
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    if port == 465:
        raise ValueError("This configuration uses STARTTLS; choose the relay's STARTTLS port, normally 587 or 25.")
    greeting_hostname = socket.getfqdn().lower()
    if '.' not in greeting_hostname:
        greeting_hostname += '.localdomain'
    hostname = host(prompt('Laptop mail hostname (SMTP greeting)', greeting_hostname))
    if '.' not in hostname:
        raise ValueError('Use a fully qualified mail hostname.')
    mechanisms = probe_relay(relay, port, hostname)
    print('Relay TLS certificate verified. Advertised authentication: ' + ', '.join(sorted(mechanisms)))
    username = prompt('SMTP username (your working Chalmers SMTP login; blank only for an approved IP relay)')
    if username and not mechanisms.intersection({'PLAIN', 'LOGIN'}):
        raise ValueError('The relay does not advertise the password mechanisms this setup supports.')
    if any(char.isspace() for char in username):
        raise ValueError('SMTP username must not contain whitespace.')
    password = getpass.getpass('SMTP password/app password: ') if username else ''
    if username and (not password or '\n' in password or '\r' in password):
        raise ValueError('Enter a nonempty single-line SMTP password.')
    print(f'\nConfigure Postfix on loopback only, relaying through [{relay}]:{port}.')
    print('TLS certificate and hostname verification will be required. Existing Postfix settings will be backed up.')
    if prompt('Type configure to install/configure Postfix') != 'configure':
        print('Cancelled.')
        return
    if not Path('/usr/sbin/postconf').exists():
        # Installation happens only after the explicit review above. No SMTP password is in argv/env.
        run(['/usr/bin/apt-get', 'update'])
        subprocess.run(['/usr/bin/apt-get', 'install', '-y', 'postfix', 'libsasl2-modules', 'ca-certificates'],
                       env={**ENV, 'DEBIAN_FRONTEND': 'noninteractive'}, check=True)
    elif username:
        subprocess.run(['/usr/bin/apt-get', 'install', '-y', 'libsasl2-modules', 'ca-certificates'],
                       env={**ENV, 'DEBIAN_FRONTEND': 'noninteractive'}, check=True)
    main_cf = Path('/etc/postfix/main.cf')
    backup = Path('/etc/postfix') / ('ubem-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(mode=0o700)
    shutil.copy2(main_cf, backup / 'main.cf')
    os.chmod(backup / 'main.cf', 0o600)
    credentials = Path('/etc/postfix/ubem_sasl_passwd')
    old_files = {}
    for path in (credentials, credentials.with_suffix('.db')):
        old_files[path] = path.read_bytes() if path.exists() else None
        if path.exists():
            shutil.copy2(path, backup / path.name)
            os.chmod(backup / path.name, 0o600)
    settings = {
        'myhostname': hostname,
        'inet_interfaces': 'loopback-only',
        'mynetworks': '127.0.0.0/8 [::1]/128',
        'mydestination': 'localhost',
        'relayhost': f'[{relay}]:{port}',
        'smtp_tls_security_level': 'verify',
        'smtp_tls_CAfile': '/etc/ssl/certs/ca-certificates.crt',
        'smtpd_relay_restrictions': 'permit_mynetworks, reject_unauth_destination',
        'smtp_sasl_auth_enable': 'yes' if username else 'no',
        'smtp_sasl_password_maps': 'hash:' + str(credentials) if username else '',
        'smtp_sasl_mechanism_filter': 'plain, login',
        'smtp_sasl_security_options': 'noanonymous',
        'smtp_sasl_tls_security_options': 'noanonymous',
    }
    try:
        if username:
            atomic(credentials, f'[{relay}]:{port} {username}:{password}\n')
            run(['/usr/sbin/postmap', str(credentials)])
            os.chmod(credentials.with_suffix('.db'), 0o600)
        for key, value in settings.items():
            run(['/usr/sbin/postconf', '-e', key + ' = ' + value])
        run(['/usr/sbin/postfix', 'check'])
        # inet_interfaces changes require a restart, not just reload.
        run(['/usr/bin/systemctl', 'restart', 'postfix'])
    except BaseException:
        shutil.copy2(backup / 'main.cf', main_cf)
        os.chmod(main_cf, 0o644)
        for path, content in old_files.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
                os.chmod(path, 0o600)
        subprocess.run(['/usr/bin/systemctl', 'restart', 'postfix'], env=ENV)
        raise
    if not username:
        credentials.unlink(missing_ok=True)
        credentials.with_suffix('.db').unlink(missing_ok=True)
    print(f'Postfix configured. Previous settings are in {backup}.')
    print('Send onboarding with manage_db_access.py, then check the queue: sudo postqueue -p')
    print('Queue acceptance does not prove delivery. Check /var/log/mail.log or journalctl for status=sent.')
    print('The SMTP relay must authorize your chosen sender address; this script cannot grant that authorization.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, smtplib.SMTPException, subprocess.CalledProcessError) as error:
        print(f'Mail setup failed: {error}', file=sys.stderr)
        sys.exit(1)
