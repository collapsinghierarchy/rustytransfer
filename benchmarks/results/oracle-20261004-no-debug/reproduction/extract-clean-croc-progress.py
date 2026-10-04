"""Retain coarse elapsed hints already printed by normal Croc progress bars."""
import argparse
import hashlib
import json
import re
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('root',type=Path)
args=p.parse_args()
rows=[json.loads(line) for line in (args.root/'raw.jsonl').read_text().splitlines()]
ANSI=re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
ELAPSED=re.compile(r'\[([^:\]]+):[^\]]*\]')
DURATION=re.compile(r'(\d+(?:\.\d+)?)(h|m|s)')
SCALE={'h':3600,'m':60,'s':1}
observations=[]
for trial in sorted((args.root/'trials').iterdir()):
    if '-croc-' not in trial.name:
        continue
    size=int(trial.name.split('mib-',1)[0])
    tag=trial.name.split('-')[2]
    index=0 if tag=='warmup' else int(tag)
    row=next(r for r in rows if r['transport']=='croc' and r['role']=='sender' and r['size_bytes']==size*1048576 and r['run_index']==index)
    source=trial/'sender.log'
    data=source.read_bytes()
    normalized=ANSI.sub('',data.decode(errors='replace'))
    displays={'source_hash':[],'data_transfer':[]}
    for line in normalized.splitlines():
        text=line.strip()
        match=ELAPSED.search(text)
        if not match:
            continue
        parts=list(DURATION.finditer(match[1]))
        if ''.join(part.group() for part in parts)!=match[1]:
            continue
        elapsed=sum(float(part[1])*SCALE[part[2]] for part in parts)
        if text.startswith('Hashing '):
            displays['source_hash'].append(dict(displayed_elapsed_seconds=elapsed,text=text))
        elif text.startswith('input-'):
            displays['data_transfer'].append(dict(displayed_elapsed_seconds=elapsed,text=text))
    retained={name:(max(values,key=lambda value:value['displayed_elapsed_seconds']) if values else None) for name,values in displays.items()}
    observations.append(dict(size_mib=size,run_index=index,warmup=row['warmup'],
        full_wall_seconds=row['wall_seconds'],sender_process_seconds=row['sender_process_seconds'],
        progress_elapsed_hints=retained,redacted_sender_log_sha256=hashlib.sha256(data).hexdigest()))
report=dict(source='Existing normal progress output from official Croc; no debug/profile flags.',
    limitation='Coarse rounded display elapsed values, potentially stale before final completion. Not exact phase boundaries, additive phases, or clean application timing instrumentation.',
    observations=observations)
(args.root/'normal-croc-progress-observations.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
