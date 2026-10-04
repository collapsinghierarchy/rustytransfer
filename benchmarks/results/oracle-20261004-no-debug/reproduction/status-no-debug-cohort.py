"""Read completed clean benchmark rows without adding work to endpoints."""
import argparse
import json
import statistics
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('root',type=Path)
args=p.parse_args()
raw=args.root/'raw.jsonl'
rows=[json.loads(line) for line in raw.read_text().splitlines()] if raw.exists() else []
sender=[r for r in rows if r['role']=='sender']
summary=[]
for size in sorted({r['size_bytes']//1048576 for r in sender}):
    for transport in ('rustytransfer','croc'):
        measured=[r for r in sender if r['transport']==transport and r['size_bytes']==size*1048576 and not r['warmup']]
        summary.append(dict(size_mib=size,transport=transport,measured_runs=len(measured),
            mean_wall_seconds=statistics.mean(r['wall_seconds'] for r in measured) if measured else None,
            mean_mib_per_second=statistics.mean(r['effective_mib_per_second'] for r in measured) if measured else None))
trials=args.root/'trials'
dirs=sorted(trials.iterdir(),key=lambda path:path.stat().st_mtime) if trials.exists() else []
errors=args.root/'errors.jsonl'
print(json.dumps(dict(completed_transfers=len(sender),summary=summary,
    newest_trial=dirs[-1].name if dirs else None,
    errors=errors.read_text().splitlines() if errors.exists() else [],
    cohort_finished=(args.root/'summary.json').exists()),indent=2))
