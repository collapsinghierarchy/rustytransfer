"""Untimed diagnostic for optional metric writer's Git provenance cost."""
import json
import subprocess
import time
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
native = Path('/home/wasilij/rustytransfer-bench')
results = []
for directory in (repo, native):
    for trial in range(3):
        measurements = []
        for command in (['git', 'rev-parse', 'HEAD'], ['git', 'status', '--porcelain']):
            started = time.monotonic()
            result = subprocess.run(command, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            measurements.append({'command':command, 'seconds':time.monotonic()-started,
                                 'returncode':result.returncode, 'output_bytes':len(result.stdout)})
        results.append({'cwd':str(directory), 'trial':trial+1, 'operations':measurements})
root = native / 'results/completion-preflight-20261004'
(root / 'metric-provenance-cost.json').write_text(json.dumps(results, indent=2)+'\n')
print(json.dumps(results, indent=2))
