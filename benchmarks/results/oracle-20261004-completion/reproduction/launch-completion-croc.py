"""Detach the authorized cohort so a chat/tool session change cannot kill it."""
import json
import subprocess
from pathlib import Path

repo=Path(__file__).resolve().parents[1]
base=Path('/home/wasilij/rustytransfer-bench')
root=base/'results/completion-croc-retry-20261004'
assert not root.exists()
log=base/'results/completion-croc-retry-20261004.launcher.log'
assert not log.exists()
command=['python3',str(repo/'target/run-completion-croc.py'),
         '--ssh-key','/home/wasilij/.ssh/id_ed25519_oracle',
         '--rusty-manifest',str(base/'build-completion-v1-20261004/manifest.json'),
         '--croc-manifest',str(base/'tools/croc-11.5.4/manifest.json'),
         '--local-input-64',str(base/'input-64.bin'),
         '--local-input-512',str(base/'input-512.bin'),
         '--remote-input-64','/home/ubuntu/rustytransfer-bench/fixtures-20261002/input-64.bin',
         '--remote-input-512','/home/ubuntu/rustytransfer-bench/fixtures-20261002/input-512.bin',
         '--output-root',str(root)]
with log.open('wb') as output:
    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=output,
                             stderr=subprocess.STDOUT,start_new_session=True)
manifest=dict(pid=process.pid,root=str(root),log=str(log),command=command,
              scope='One complete, direct-only retry of the interrupted Croc comparison; sequential and detached from chat tool lifetime.')
(base/'results/completion-croc-retry-20261004.launcher.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(manifest))
