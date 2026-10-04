"""A bounded, source-IP-only Oracle ingress lease for direct Croc tests."""
import ipaddress
import json
import shlex
import subprocess
import time
import uuid

import run_oracle_transfer as runner

LEASE_CODE = r'''
import hashlib, ipaddress, json, os, signal, subprocess, sys, time
from pathlib import Path
cidr, tag, root, lease_seconds = sys.argv[1:]
lease_seconds = int(lease_seconds)
assert 1 <= lease_seconds <= 1800
ipaddress.IPv4Network(cidr)
assert tag.startswith('rt-croc-') and Path(root).name == tag
iptables = '/usr/sbin/iptables'
rule = ['-s', cidr, '-p', 'tcp', '--dport', '9009:9013', '-m', 'comment', '--comment', tag, '-j', 'ACCEPT']
before = subprocess.check_output([iptables, '-S', 'INPUT'])
stop_requested = False
def stop(_signal, _frame):
    global stop_requested
    stop_requested = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
(Path(root) / 'pid.json').write_text(json.dumps({'pid': os.getpid(), 'tag': tag, 'cidr': cidr,
    'root': root, 'lease_seconds': lease_seconds}))
try:
    subprocess.run([iptables, '-I', 'INPUT', '1', *rule], check=True)
    (Path(root) / 'ready.json').write_text(json.dumps({'pid': os.getpid(), 'tag': tag, 'cidr': cidr,
        'root': root, 'lease_seconds': lease_seconds}))
    deadline = time.monotonic() + lease_seconds
    while not stop_requested and time.monotonic() < deadline:
        time.sleep(0.5)
finally:
    if subprocess.run([iptables, '-C', 'INPUT', *rule], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        subprocess.run([iptables, '-D', 'INPUT', *rule], check=True)
    after = subprocess.check_output([iptables, '-S', 'INPUT'])
    (Path(root) / 'restored.json').write_text(json.dumps({'tag': tag, 'cidr': cidr, 'exact_input_chain_restored': before == after,
        'before_sha256': hashlib.sha256(before).hexdigest(), 'after_sha256': hashlib.sha256(after).hexdigest(),
        'temporary_tcp_ports': [9009, 9010, 9011, 9012, 9013], 'source_restriction': 'single-client IPv4 /32',
        'maximum_lease_seconds': lease_seconds}))
'''

STOP_CODE = r'''
import json, os, signal, sys
from pathlib import Path
root = Path(sys.argv[1])
data = json.loads((root / 'pid.json').read_text())
assert data['tag'] == root.name and data['tag'].startswith('rt-croc-')
assert data.get('root', str(root)) == str(root)
pid = data['pid']
proc = Path('/proc') / str(pid)
if proc.exists():
    assert os.readlink(proc / 'cwd') == str(root)
    args = (proc / 'cmdline').read_bytes().split(b'\0')
    argv = [part.decode() for part in args if part]
    legacy = [data['cidr'], data['tag'], str(root)]
    configured = legacy + [str(data['lease_seconds'])] if 'lease_seconds' in data else None
    assert (argv[-3:] == legacy and configured is None) or (configured is not None and argv[-4:] == configured)
    if configured is not None:
        assert 1 <= int(data['lease_seconds']) <= 1800
    os.kill(pid, signal.SIGTERM)
'''


class OracleFirewallLease:
    def __init__(self, args, audit_path, lease_seconds=1200):
        if not 1 <= lease_seconds <= 1800:
            raise ValueError('lease_seconds must be between 1 and 1800')
        self.args = args
        self.audit_path = audit_path
        self.lease_seconds = lease_seconds
        self.tag = 'rt-croc-' + uuid.uuid4().hex[:12]
        self.root = '/home/ubuntu/rustytransfer-bench/' + self.tag
        self.process = None
        self.cidr = None
        self.fallback_used = False

    def _stop_launcher(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def _write_ttl_fallback_audit(self, reason):
        self.fallback_used = True
        self.audit_path.write_text(json.dumps({
            'tag': self.tag,
            'cidr': self.cidr,
            'exact_input_chain_restored': None,
            'cleanup': 'ssh_unavailable; remote TTL remains the fallback',
            'maximum_lease_seconds': self.lease_seconds,
            'cleanup_error': str(reason),
        }, indent=2) + '\n')

    def _stop_remote_lease(self):
        stop = 'sudo -n /usr/bin/python3 -c ' + shlex.quote(STOP_CODE) + ' ' + shlex.quote(self.root)
        last_error = None
        for attempt in range(3):
            try:
                runner.remote(self.args, stop)
                break
            except Exception as error:
                last_error = error
                if attempt < 2:
                    time.sleep(0.25)
        else:
            try:
                runner.remote(self.args, 'true')
            except Exception as connection_error:
                self._stop_launcher()
                self._write_ttl_fallback_audit(connection_error)
                return
            no_pid = runner.remote(
                self.args,
                f'test ! -e {shlex.quote(self.root + "/pid.json")} && '
                f'test ! -e {shlex.quote(self.root + "/ready.json")} && echo no-lease',
            )
            if no_pid == 'no-lease' and self.process is not None and self.process.poll() is not None:
                runner.remote(self.args, f'rmdir -- {shlex.quote(self.root)}')
                return
            raise RuntimeError(f'Oracle firewall stop failed while SSH remained available: {last_error}')

        if self.process is not None:
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError('Oracle firewall lease did not exit after its scoped stop request') from error
        restored = runner.read_remote_json(self.args, self.root + '/restored.json')
        self.audit_path.write_text(json.dumps(restored, indent=2) + '\n')
        if not restored['exact_input_chain_restored']:
            raise RuntimeError('Oracle INPUT chain differs after scoped lease cleanup')
        runner.remote(
            self.args,
            'rm -- ' + ' '.join(
                shlex.quote(self.root + '/' + name)
                for name in ('pid.json', 'ready.json', 'restored.json')
            ) + ' && rmdir -- ' + shlex.quote(self.root),
        )

    def __enter__(self):
        source = runner.remote(self.args, 'printenv SSH_CONNECTION').split()[0]
        self.cidr = str(ipaddress.IPv4Address(source)) + '/32'
        runner.remote(self.args, f'mkdir -- {shlex.quote(self.root)}')
        command = (f'cd {shlex.quote(self.root)} && exec setsid --wait sudo -n /usr/bin/python3 -c '
            f'{shlex.quote(LEASE_CODE)} {shlex.quote(self.cidr)} {shlex.quote(self.tag)} '
            f'{shlex.quote(self.root)} {self.lease_seconds}')
        self.process = subprocess.Popen(runner.ssh_command(self.args, command), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('Scoped firewall lease exited before readiness: ' + self.process.stderr.read().decode())
                ready = runner.remote(self.args, f'test ! -e {shlex.quote(self.root + "/ready.json")} || cat -- {shlex.quote(self.root + "/ready.json")}')
                if ready:
                    data = json.loads(ready)
                    assert data['cidr'] == self.cidr and data['tag'] == self.tag
                    assert data.get('lease_seconds') == self.lease_seconds
                    self.args.croc_direct_peer_ip = str(ipaddress.IPv4Address(source))
                    return self
                time.sleep(0.1)
            raise TimeoutError('Scoped firewall lease did not become ready; TTL restoration remains bounded')
        except Exception as entry_error:
            try:
                self._stop_remote_lease()
            except Exception as cleanup_error:
                entry_error.add_note(f'firewall lease cleanup also failed: {cleanup_error}')
            raise

    def __exit__(self, exc_type, value, _traceback):
        try:
            self._stop_remote_lease()
        except Exception as cleanup_error:
            if exc_type is None:
                raise
            value.add_note(f'firewall lease cleanup also failed: {cleanup_error}')
        if self.fallback_used and exc_type is None:
            raise RuntimeError('SSH was unavailable; firewall lease cleanup is bounded by its remote TTL')
        return False
