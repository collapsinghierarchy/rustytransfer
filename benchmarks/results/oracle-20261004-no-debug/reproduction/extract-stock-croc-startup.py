"""Retain the precise existing stock Croc startup milestones from redacted logs."""
import argparse
import hashlib
import json
import math
import re
import statistics
from pathlib import Path

EVENT = re.compile(r'startup milestone ([a-z0-9-]+) elapsed=([^\s]+)')
PART = re.compile(r'(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)')
SCALE = {'ns':1e-9, 'us':1e-6, 'µs':1e-6, 'μs':1e-6, 'ms':1e-3, 's':1, 'm':60, 'h':3600}


def duration(text):
    parts = list(PART.finditer(text))
    assert ''.join(part.group() for part in parts) == text, text
    result = sum(float(part[1]) * SCALE[part[2]] for part in parts)
    assert math.isfinite(result) and result >= 0
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    args = p.parse_args()
    records = []
    for logs in sorted(args.root.glob('oracle-*/logs/croc-oracle-to-wsl-*')):
        size = int(logs.name.split('-')[4].removesuffix('mib'))
        tag = logs.name.split('-')[-1]
        endpoints = {}
        for role in ('sender', 'receiver'):
            path = logs / (role + '.log')
            data = path.read_bytes()
            assert not re.search(rb'rtoracle-[^\s\'\"]+|rt1:\S+', data), path
            events = {}
            for name, value in EVENT.findall(data.decode()):
                assert name not in events, (path, name)
                events[name] = duration(value)
            assert events.get('process-start') == 0 and 'transport-ready' in events, path
            endpoints[role] = dict(milestone_elapsed_seconds=events,
                                   redacted_log_sha256=hashlib.sha256(data).hexdigest())
        records.append(dict(size_mib=size, run_index=0 if tag == 'warmup' else int(tag),
                            warmup=tag == 'warmup', endpoints=endpoints))
    summary = []
    for size in sorted({r['size_mib'] for r in records}):
        selected = [r for r in records if r['size_mib'] == size and not r['warmup']]
        if not selected:
            summary.append(dict(size_mib=size, measured_count=0, endpoints={},
                                note='Only an unscored warmup was retained.'))
            continue
        groups = {}
        for role in ('sender', 'receiver'):
            names = sorted(set.intersection(*(set(r['endpoints'][role]['milestone_elapsed_seconds']) for r in selected)))
            groups[role] = {name:dict(mean_seconds=statistics.mean(r['endpoints'][role]['milestone_elapsed_seconds'][name] for r in selected),
                                     measured_count=len(selected)) for name in names}
        summary.append(dict(size_mib=size, endpoints=groups))
    report = dict(source='Existing debug startupTiming milestones from official Croc 11.5.4, no added instrumentation.',
                  limitation='Elapsed values share the Croc startupTiming origin labelled process-start; milestones are semantic events, not an additive phase decomposition. They do not identify exact payload completion.',
                  records=records, summary=summary)
    (args.root / 'croc-stock-startup.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
