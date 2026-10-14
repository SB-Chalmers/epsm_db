"""Remote delivery and execution supervisor. All progression is receipt-gated."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import subprocess
import tarfile
import threading
import time
from uuid import uuid4

GIB = 1024**3


class GateFailure(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(8*1024**2), b''):
            digest.update(data)
    return digest.hexdigest()


def publish(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2, default=str) + '\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def check_capacity(path, reserve):
    available = shutil.disk_usage(path).free
    if available < reserve:
        raise GateFailure(f'Free disk below reserve: {available} bytes available; {reserve} required')
    return available


def safe_member(member, root, names):
    name = Path(member.name)
    if name.is_absolute() or '..' in name.parts or not name.parts:
        raise GateFailure('Unsafe archive member: ' + member.name)
    target = root / name
    if not target.resolve().is_relative_to(root.resolve()):
        raise GateFailure('Archive member escapes staging')
    if member.issym() or not (member.isdir() or member.isfile() or member.islnk()):
        raise GateFailure('Unsupported archive member: ' + member.name)
    if member.islnk():
        link = Path(member.linkname)
        if link.is_absolute() or '..' in link.parts or str(link) not in names:
            raise GateFailure('Unsafe or unresolved archive hard link')
    return target


def extract_archive(archive, staging):
    staging.mkdir(parents=True, exist_ok=False)
    process = subprocess.Popen(['zstd', '-dc', str(archive)], stdout=subprocess.PIPE)
    names = set()
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as stream:
            for member in stream:
                target = safe_member(member, staging, names)
                if member.name in names:
                    raise GateFailure('Duplicate archive member: ' + member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.islnk():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.link(staging / member.linkname, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('xb') as destination:
                        shutil.copyfileobj(stream.extractfile(member), destination, length=8*1024**2)
                    target.chmod(member.mode & 0o755)
                names.add(member.name)
        process.stdout.close()
        if process.wait() != 0:
            raise GateFailure('Zstandard extraction failed')
    except BaseException:
        process.kill(); process.wait()
        raise


def verify_transport(root, expected_count):
    manifest = json.loads((root/'transport-file-manifest.json').read_text())
    if len(manifest['files']) != expected_count:
        raise GateFailure('Transport manifest file count changed')
    cache = {}
    for row in manifest['files']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise GateFailure('Unsafe manifest path')
        path = root/relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise GateFailure('Missing or escaping file: ' + str(relative))
        before = path.stat()
        identity = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        key = identity(before)
        if key not in cache:
            cache[key] = sha256(path)
        if identity(path.stat()) != key or before.st_size != row['bytes'] or cache[key] != row['sha256']:
            raise GateFailure('Transport checksum changed: ' + str(relative))
    return dict(file_occurrences=len(manifest['files']), unique_assets=len(cache))


class Pipeline:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['root']).resolve()
        self.state = self.root/'runs'/config['release_id']
        self.state.mkdir(parents=True, exist_ok=True)
        self.release = self.root/'releases'/config['release_id']
        self.kit = Path(__file__).resolve().parent
        self.started = time.monotonic()
        self.stage = 'starting'
        self.process = None
        self.stopped = threading.Event()
        self.env = os.environ.copy()
        self.env.update(UBEM_MIN_FREE_BYTES=str(config['reserve_bytes']),
                        UBEM_CAPACITY_GUARD_PATH=str(self.root), UBEM_BATCH_ROWS='8192',
                        UBEM_BATCH_BYTES=str(64*1024**2), UBEM_SOURCE_PROFILE='linux-server',
                        OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
        self.config_digest = hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
        self.receipts = self.state/'receipts'
        self.receipts.mkdir(exist_ok=True)

    def control(self):
        path=self.state/'control.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def status(self, **extra):
        value=dict(stage=self.stage, timestamp=datetime.now(timezone.utc).isoformat(),
                   pid=os.getpid(), elapsed_seconds=time.monotonic()-self.started,
                   free_bytes=shutil.disk_usage(self.root).free,
                   config_digest=self.config_digest, **extra)
        publish(self.state/'status.json', value)
        print(json.dumps(value,default=str),flush=True)

    def heartbeat(self):
        while not self.stopped.wait(30):
            extra={}
            if self.process and self.process.poll() is None:
                extra['worker_pid']=self.process.pid
                proc=Path('/proc')/str(self.process.pid)/'status'
                if proc.exists():
                    for line in proc.read_text().splitlines():
                        if line.startswith('VmRSS:'):
                            extra['worker_rss_bytes']=int(line.split()[1])*1024
                if self.control().get('paused') or shutil.disk_usage(self.root).free < self.config['reserve_bytes']:
                    os.killpg(self.process.pid,signal.SIGTERM)
                    extra['stop_requested']='paused or free-space reserve reached'
            self.status(**extra)
            if (self.release/'.venv/bin/python').exists() and (self.root/'runtime/postgres.env').exists():
                try:
                    self.call_task('status', heartbeat=True)
                except Exception:
                    pass

    def command(self, command, *, cwd=None, env=None, stdin=None, log_name=None):
        if self.control().get('paused'):
            raise GateFailure('Paused by operator; checkpoints retained')
        check_capacity(self.root,self.config['reserve_bytes'])
        with (self.state/(log_name or self.stage+'.log')).open('a') as log:
            self.process=subprocess.Popen(list(map(str,command)),cwd=cwd,env=env or self.env,
                                          stdin=stdin,stdout=log,stderr=log,start_new_session=True)
            returncode=self.process.wait()
        self.process=None
        if returncode:
            raise GateFailure(f'{self.stage} command failed with exit {returncode}; inspect its log')

    def step(self,name,action):
        self.stage=name;self.status()
        receipt=self.receipts/(name+'.json')
        if receipt.exists():
            value=json.loads(receipt.read_text())
            if value.get('config_digest')!=self.config_digest or not value.get('passed'):
                raise GateFailure('Existing stage receipt conflicts: '+name)
            return value.get('result')
        result=action()
        if self.control().get('paused'):
            raise GateFailure('Paused by operator')
        publish(receipt,dict(passed=True,config_digest=self.config_digest,
                            timestamp=datetime.now(timezone.utc).isoformat(),result=result))
        return result

    def wait_for_delivery(self):
        archive=Path(self.config['remote_archive'])
        marker=Path(str(archive)+'.ready')
        while not marker.exists():
            if self.control().get('paused'):
                raise GateFailure('Paused by operator')
            self.status(received_bytes=archive.stat().st_size if archive.exists() else 0,
                        expected_bytes=self.config['archive_bytes'])
            time.sleep(30)
        value=json.loads(marker.read_text())
        if value != {k:self.config[k] for k in ('archive_sha256','archive_bytes')}:
            raise GateFailure('Delivery marker conflicts with configured release')
        if archive.stat().st_size!=self.config['archive_bytes'] or sha256(archive)!=self.config['archive_sha256']:
            raise GateFailure('Delivered archive size/SHA-256 mismatch')
        self.command(['zstd','-t',archive])
        return value

    def delivery(self):
        if self.release.exists():
            marker=self.release/'delivery-receipt.json'
            if not marker.exists() or json.loads(marker.read_text()).get('archive_sha256')!=self.config['archive_sha256']:
                raise GateFailure('Conflicting accepted release destination')
            return json.loads(marker.read_text())
        check_capacity(self.root,self.config['reserve_bytes']+self.config['uncompressed_bytes'])
        staging=self.root/'releases'/('.staging-'+uuid4().hex)
        extract_archive(Path(self.config['remote_archive']),staging)
        result=verify_transport(staging,self.config['file_occurrences'])
        result.update(archive_sha256=self.config['archive_sha256'],archive_bytes=self.config['archive_bytes'],
                      timestamp=datetime.now(timezone.utc).isoformat())
        publish(staging/'delivery-receipt.json',result)
        if self.release.exists():
            raise GateFailure('Release destination appeared during verification')
        staging.rename(self.release)
        return result

    def administrator(self):
        while True:
            docker=subprocess.run(['sh','-c','command -v docker >/dev/null && docker info >/dev/null 2>&1'],capture_output=True)
            linger=subprocess.run(['loginctl','show-user',str(os.getuid()),'-p','Linger','--value'],capture_output=True,text=True)
            venv=subprocess.run(['python3.12','-c','import venv,ensurepip'],capture_output=True)
            if docker.returncode==0 and linger.stdout.strip()=='yes' and venv.returncode==0:
                return dict(docker_access=True,linger=True,python_venv=True)
            if self.control().get('paused'):
                raise GateFailure('Paused by operator')
            self.status(action_required='Run the deployed administrator bootstrap once on the laptop',
                        bootstrap=str(self.kit/'bootstrap-admin.sh'))
            time.sleep(30)

    def environment(self):
        runtime=self.root/'runtime';runtime.mkdir(exist_ok=True)
        envfile=runtime/'postgres.env'
        if not envfile.exists():
            descriptor=os.open(envfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w') as stream:
                stream.write('UBEM_POSTGRES_PASSWORD='+secrets.token_urlsafe(32)+'\n')
        password=envfile.read_text().strip().split('=',1)[1]
        self.env.update(UBEM_DATABASE_URL='postgresql://ubem_owner:'+password+'@127.0.0.1:55433/sweden_ubem_v004_baseline',
                        UBEM_SOURCE_ROOT=str(self.release/'sources'),UBEM_TRANSPORT_ROOT=str(self.release),
                        UBEM_REMOTE_STATE=str(self.state),UBEM_REMOTE_ROOT=str(self.root),
                        JUPYTER_PATH=str(self.release/'.venv/share/jupyter'))

    def runtime(self):
        self.environment()
        compose=self.root/'runtime/compose.yaml'
        text=(self.kit/'compose.yaml').read_text().replace('__UBEM_ROOT__',str(self.root))
        if compose.exists() and compose.read_text()!=text:
            raise GateFailure('Conflicting runtime compose configuration')
        compose.write_text(text)
        self.command(['docker','compose','--env-file',compose.parent/'postgres.env','-p','ubem-national','-f',compose,'up','-d','--wait'])
        venv=self.release/'.venv'
        if not (venv/'bin/python').exists():
            self.command(['python3.12','-m','venv',venv])
        self.command([venv/'bin/pip','install','-c',self.release/'requirements-tested.txt','-e',str(self.release/'application')+'[notebook]','pytest>=8,<10'])
        self.command([venv/'bin/python','-m','ipykernel','install','--prefix',venv,'--name','python3','--display-name','UBEM national'])
        return dict(database='sweden_ubem_v004_baseline',port=55433,image=self.config['image'])

    def call_task(self,task,heartbeat=False):
        command=[self.release/'.venv/bin/python',self.kit/'remote_tasks.py',task,'--config',self.kit/'config.json']
        environment=self.env.copy()
        environment['PYTHONPATH']=str(self.release/'application/src')
        if heartbeat:
            subprocess.run(list(map(str,command)),env=environment,cwd=self.release/'application',
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,check=True)
        else:
            self.recover_disconnected_sources()
            self.command(command,env=environment,cwd=self.release/'application')
        result=self.state/(task+'-result.json')
        return json.loads(result.read_text()) if result.exists() else {}

    def recover_disconnected_sources(self):
        root=Path(self.env['UBEM_SOURCE_ROOT'])
        hidden=root.with_name(root.name+'.acceptance-disconnected')
        marker=self.state/'source-directory-identity.json'
        if root.exists():
            stat=root.stat()
            publish(marker,dict(device=stat.st_dev,inode=stat.st_ino,inventory_sha256=sha256(root/'manifests/file-inventory.json')))
        elif hidden.exists() and marker.exists():
            saved=json.loads(marker.read_text());stat=hidden.stat()
            if (stat.st_dev,stat.st_ino)!=(saved['device'],saved['inode']) or sha256(hidden/'manifests/file-inventory.json')!=saved['inventory_sha256']:
                raise GateFailure('Disconnected source directory identity changed')
            hidden.rename(root)
        else:
            raise GateFailure('Source root is missing without an owned disconnection receipt')

    def run(self):
        lock=(self.state/'pipeline.lock').open('a')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        deployment=self.kit.parent/(self.kit.name+'.deployment.json')
        if deployment.exists():
            expected=json.loads(deployment.read_text())['kit_manifest_sha256']
            if sha256(self.kit/'kit-manifest.json')!=expected:
                raise GateFailure('Execution kit manifest changed after deployment')
            manifest=json.loads((self.kit/'kit-manifest.json').read_text())
            for row in manifest['files']:
                path=self.kit/row['path']
                if sha256(path)!=row['sha256']:
                    raise GateFailure('Execution kit file changed after deployment: '+row['path'])
        threading.Thread(target=self.heartbeat,daemon=True).start()
        try:
            self.step('waiting_for_delivery',self.wait_for_delivery)
            self.step('verify_and_publish_delivery',self.delivery)
            self.step('waiting_for_administrator',self.administrator)
            self.step('install_runtime',self.runtime)
            self.environment()
            for name in ['preflight','restore_reference','import_sources','reconcile_sources','uddevalla_acceptance',
                         'processing_capacity','process_national','acquire_scb','repair_heights','enriched_build','final_restore_and_publish']:
                self.step(name,lambda name=name:self.call_task(name))
            self.stage='completed';self.status()
            return 0
        except Exception as error:
            failed=self.stage;self.stage='attention_required'
            self.status(failed_stage=failed,error=str(error),publication_accepted=False)
            return 78
        finally:
            self.stopped.set()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['run','status','pause','resume'])
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args();pipeline=Pipeline(json.loads(args.config.read_text()))
    if args.action=='status':
        path=pipeline.state/'status.json'
        print(path.read_text() if path.exists() else '{}');return 0
    if args.action=='pause':
        publish(pipeline.state/'control.json',dict(paused=True));return 0
    if args.action=='resume':
        publish(pipeline.state/'control.json',dict(paused=False))
        subprocess.run(['systemctl','--user','reset-failed','ubem-remote.service'],check=True)
        subprocess.run(['systemctl','--user','restart','ubem-remote.service'],check=True)
        return 0
    try:
        return pipeline.run()
    except BlockingIOError:
        print('Pipeline already running; refusing a concurrent worker')
        return 0
    except GateFailure as error:
        pipeline.stage='attention_required';pipeline.status(error=str(error))
        return 78


if __name__=='__main__':
    raise SystemExit(main())
