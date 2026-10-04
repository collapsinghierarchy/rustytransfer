"""Retrieve already-completed build logs outside benchmark intervals."""
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'benchmarks'))
import run_oracle_transfer as runner

args = SimpleNamespace(ssh='ssh', ssh_key=Path('/home/wasilij/.ssh/id_ed25519_oracle'), user='ubuntu', host='141.147.1.21')
for name in sys.argv[1:]:
    assert name.replace('-', '').isalnum()
    destination = Path('/home/wasilij/rustytransfer-bench') / ('build-' + name) / 'arm-build.log'
    assert destination.parent.is_dir() and not destination.exists()
    text = runner.remote(args, 'cat -- ' + runner.remote_quote('/home/ubuntu/rustytransfer-bench/build-' + name + '/arm-build.log'))
    destination.write_text(text + '\n')
    print('Retained ARM build log:', name)
