"""Freeze current payload sources, build sequentially, and record exact binaries."""
import argparse
import gzip
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)
sys.path.insert(0, str(REPO / 'benchmarks'))
import run_oracle_transfer as runner

p = argparse.ArgumentParser()
p.add_argument('name')
p.add_argument('--example')
cli = p.parse_args()
if not cli.name.replace('-', '').isalnum():
    p.error('name must be alphanumeric with hyphens')
base = Path('/home/wasilij/rustytransfer-bench')
remote_base = '/home/ubuntu/rustytransfer-bench'
root = base / ('build-' + cli.name)
root.mkdir(exist_ok=False)
args = SimpleNamespace(ssh='ssh', scp='scp', ssh_key=Path('/home/wasilij/.ssh/id_ed25519_oracle'), user='ubuntu', host='141.147.1.21')
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
paths = subprocess.check_output(['git', 'ls-files', 'Cargo.toml', 'Cargo.lock', '.cargo', 'src', 'crates', 'examples'], text=True).splitlines()
files = {}
archive = root / 'source.tar.gz'
with archive.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as gz, tarfile.open(fileobj=gz, mode='w') as tar:
    for name in sorted(paths):
        data = (REPO / name).read_bytes()
        member = tarfile.TarInfo(name)
        member.size = len(data)
        member.mode = 0o644
        tar.addfile(member, io.BytesIO(data))
        files[name] = hashlib.sha256(data).hexdigest()
remote_root = remote_base + '/build-' + cli.name
runner.remote(args, f'test ! -e {shlex.quote(remote_root)} && mkdir -- {shlex.quote(remote_root)}')
runner.copy_to_remote(args, archive, remote_root + '/source.tar.gz')
assert runner.remote_sha256(args, remote_root + '/source.tar.gz') == runner.sha256_file(archive)
runner.remote(args, f'mkdir -- {shlex.quote(remote_root + "/source")} && tar -xzf {shlex.quote(remote_root + "/source.tar.gz")} -C {shlex.quote(remote_root + "/source")}')
runner.remote(args, f'find {shlex.quote(remote_root + "/source")} -type f -exec touch -- {{}} +')
local_source = root / 'source'
local_source.mkdir()
with tarfile.open(archive, 'r:gz') as tar:
    tar.extractall(local_source, filter='data')
for name in paths:
    os.utime(local_source / name, None)
env = os.environ.copy()
env['CARGO_TARGET_DIR'] = str(base / 'target')
env['CARGO_BUILD_JOBS'] = '4'
print('Building frozen x86 source: ' + cli.name, flush=True)
build_command = ['/home/wasilij/.cargo/bin/cargo', 'build', '--release', '--locked', '-p', 'rustytransfer']
if cli.example:
    build_command.extend(['--example', cli.example])
with (root / 'x86-build.log').open('w') as log:
    subprocess.run(build_command, cwd=local_source, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
local_binary = root / 'rustytransfer-x86'
artifact_suffix = 'examples/' + cli.example if cli.example else 'rustytransfer'
shutil.copy2(base / 'target/release' / artifact_suffix, local_binary)
print('Building frozen ARM source: ' + cli.name, flush=True)
remote_binary = remote_root + '/rustytransfer-arm'
example_flag = ' --example ' + shlex.quote(cli.example) if cli.example else ''
runner.remote(args, f'cd {shlex.quote(remote_root + "/source")} && CARGO_TARGET_DIR={remote_base}/source-4ba7b01/target CARGO_BUILD_JOBS=1 /home/ubuntu/.cargo/bin/cargo build --release --locked -p rustytransfer{example_flag} > {shlex.quote(remote_root + "/arm-build.log")} 2>&1 && cp -- {shlex.quote(remote_base + "/source-4ba7b01/target/release/" + artifact_suffix)} {shlex.quote(remote_binary)}')
manifest = {'frozen_at_utc': datetime.now(timezone.utc).isoformat(), 'source_commit': commit,
    'artifact_kind': 'example:' + cli.example if cli.example else 'production-cli',
    'source_dirty': bool(subprocess.check_output(['git', 'diff', '--', '.cargo', 'Cargo.toml', 'Cargo.lock', 'src', 'crates', 'examples'], text=True)),
    'source_archive_sha256': runner.sha256_file(archive), 'source_files_sha256': files,
    'x86_binary': str(local_binary), 'x86_binary_sha256': runner.sha256_file(local_binary),
    'arm_binary': remote_binary, 'arm_binary_sha256': runner.remote_sha256(args, remote_binary),
    'rustc_x86': subprocess.check_output(['/home/wasilij/.cargo/bin/rustc', '-Vv'], text=True),
    'rustc_arm': runner.remote(args, '/home/ubuntu/.cargo/bin/rustc -Vv')}
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps({k: v for k, v in manifest.items() if k != 'source_files_sha256'}, indent=2), flush=True)
