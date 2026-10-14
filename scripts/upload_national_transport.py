"""Persistent, resumable upload of one pinned transport; never publishes it."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time


def publish(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def upload(config):
    state = Path(config['state_directory'])
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / 'upload.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    archive = Path(config['archive'])
    target = config['remote_archive']
    started = time.monotonic()
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'Compression=no',
           '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', config['ssh_host']]

    def report(stage, **details):
        value = dict(stage=stage, timestamp=datetime.now(timezone.utc).isoformat(),
                     elapsed_seconds=time.monotonic() - started,
                     expected_bytes=config['archive_bytes'], target=config['ssh_host'], **details)
        publish(state / 'upload-status.json', value)
        print(json.dumps(value), flush=True)

    def paused():
        control = state / 'upload-control.json'
        return control.exists() and json.loads(control.read_text()).get('paused', False)

    if paused():
        report('paused')
        return 0
    report('verifying_local_archive')
    before = archive.stat()
    if before.st_size != config['archive_bytes'] or sha256(archive) != config['archive_sha256']:
        raise ValueError('Local archive differs from the verified transport receipt')
    after = archive.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if identity(before) != identity(after):
        raise ValueError('Local archive changed during verification')
    subprocess.run(ssh + ['mkdir -p ' + shlex.quote(str(Path(target).parent))], check=True)
    # Only an unpublished incoming file is writable. Full remote SHA gates publication.
    # GNU append-verify avoids a delta scan of the compressed archive on resume.
    # It verifies the complete result; the delivery gate also checks SHA-256.
    rsync = config.get('rsync_binary', '/opt/homebrew/bin/rsync')
    help_text = subprocess.run([rsync, '--help'], capture_output=True, text=True, check=True).stdout
    if '--append-verify' not in help_text:
        raise ValueError('Upload requires GNU rsync with --append-verify support')
    command = [rsync, '--partial', '--append-verify', '--timeout=120',
               '-e', shlex.join(ssh[:-1]), str(archive), config['ssh_host'] + ':' + target]
    for attempt, delay in enumerate([0, 30, 120, 300], 1):
        if delay:
            report('retry_wait', attempt=attempt, retry_seconds=delay)
            for _ in range(delay):
                if paused():
                    report('paused'); return 0
                time.sleep(1)
        if paused():
            report('paused'); return 0
        report('uploading', attempt=attempt)
        with (state / 'rsync.log').open('a') as log:
            process = subprocess.Popen(command, stdout=log, stderr=log)
            previous_bytes, previous_time = None, None
            while process.poll() is None:
                if paused():
                    process.terminate(); process.wait()
                    report('paused'); return 0
                try:
                    result = subprocess.run(ssh + ['stat -c %s -- ' + shlex.quote(target)],
                                            capture_output=True, text=True, timeout=20)
                    if result.returncode != 0:
                        raise ValueError('Remote progress query failed')
                    current_bytes = int(result.stdout)
                    now = time.monotonic()
                    rate = None if previous_bytes is None else max(0, current_bytes - previous_bytes) / (now - previous_time)
                    report('uploading', attempt=attempt, remote_bytes=current_bytes,
                           bytes_per_second=rate,
                           eta_seconds=(config['archive_bytes'] - current_bytes) / rate if rate else None)
                    previous_bytes, previous_time = current_bytes, now
                except (ValueError, subprocess.TimeoutExpired):
                    report('uploading', attempt=attempt, remote_progress_unavailable=True)
                for _ in range(30):
                    if process.poll() is not None or paused():
                        break
                    time.sleep(1)
        if process.returncode == 0:
            if identity(archive.stat()) != identity(after):
                raise ValueError('Local archive changed during upload')
            marker = dict(archive_sha256=config['archive_sha256'], archive_bytes=config['archive_bytes'])
            marker_command = 'printf %s ' + shlex.quote(json.dumps(marker)) + ' > ' + shlex.quote(target + '.ready.tmp') + ' && mv -- ' + shlex.quote(target + '.ready.tmp') + ' ' + shlex.quote(target + '.ready')
            subprocess.run(ssh + [marker_command], check=True)
            report('uploaded_awaiting_remote_verification', remote_bytes=config['archive_bytes'])
            return 0
    report('attention_required', reason='Transfer failed after bounded retries; partial file retained')
    return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    try:
        raise SystemExit(upload(config))
    except Exception as error:
        publish(Path(config['state_directory']) / 'upload-status.json',
                dict(stage='attention_required', error=str(error), timestamp=datetime.now(timezone.utc).isoformat()))
        raise
