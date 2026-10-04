"""Prepare independent-connection cohorts; never run transfers while preparing."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('phase', choices=('screen', 'confirm', 'guards'))
parser.add_argument('--candidate', required=True, type=Path)
args = parser.parse_args()
base = Path('/home/wasilij/rustytransfer-bench')
candidate = json.loads(args.candidate.read_text())
assert candidate['artifact_kind'] == 'example:shared_key_parallel'
variants = {
    f'c{connections}-s{streams}': dict(candidate, chunk_size=262144,
        experimental_streams=streams, experimental_connections=connections,
        experimental_protocol_version='shared-key-parallel/2',
        build_id='shared-key-independent-connections-v2')
    for connections, streams in ((1, 1), (1, 4), (4, 4))
}
series = 2 if args.phase == 'confirm' else 1
if args.phase == 'guards':
    groups = [dict(name=f'guard-{direction}-{size}', series=1,
                   direction=direction, size_mib=size,
                   variants=['c1-s1', 'c4-s4'], runs=10 if size < 1 else 5)
              for direction, size in (('oracle-to-wsl', 64), ('wsl-to-oracle', 512),
                                      ('wsl-to-oracle', 64), ('oracle-to-wsl', 0.0625),
                                      ('wsl-to-oracle', 0.0625))]
else:
    groups = [dict(name=f's{series}-independent-oracle-to-wsl-512', series=series,
                   direction='oracle-to-wsl', size_mib=512,
                   variants=list(variants), runs=5)]
name = 'independent-connections-' + args.phase + '-20261004'
config = base / ('matrix-' + name + '.json')
assert not config.exists()
config.write_text(json.dumps({'output_root': str(base / 'results' / name),
                              'variants': variants, 'groups': groups}, indent=2) + '\n')
print(config)
