"""Read retained rows without contacting endpoints or creating load."""
import argparse
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('root', type=Path)
args = p.parse_args()
for path in sorted(args.root.rglob('rows.jsonl')):
    print(path.relative_to(args.root))
    for line in path.read_text().splitlines():
        row = json.loads(line)
        profile = row.get('completion_profile', {})
        fields = {key: round(value, 4) for key, value in profile.items()
                  if key.endswith('_seconds')}
        print(row['run_index'], row['warmup'], row['role'],
              round(row['effective_mib_per_second'], 3),
              'wall', round(row['wall_seconds'], 4),
              'payload', round(row['payload_seconds'], 4),
              'shutdown', round(row['shutdown_seconds'], 4),
              'process', row.get('endpoint_process_seconds'), fields)
