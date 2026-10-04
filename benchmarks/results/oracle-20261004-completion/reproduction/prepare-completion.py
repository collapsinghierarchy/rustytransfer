"""Prepare the preregistered completion experiment without running transfers."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('phase', choices=('diagnostics', 'screen', 'confirm', 'guards', 'overhead'))
parser.add_argument('--build', required=True, type=Path)
args = parser.parse_args()
base = Path('/home/wasilij/rustytransfer-bench')
build = json.loads(args.build.read_text())
assert build['artifact_kind'] == 'production-cli'
variants = {
    name: dict(build, chunk_size=262144, completion_profile=True,
               explicit_connection_close=enabled, endpoint_cwd=str(base),
               build_id='completion-v1-' + name)
    for name, enabled in (('baseline', False), ('explicit-close', True))
}
series = 2 if args.phase == 'confirm' else 1
if args.phase == 'diagnostics':
    variants['baseline-workspace'] = dict(variants['baseline'],
        endpoint_cwd=str(Path(__file__).resolve().parents[1]),
        build_id='completion-v1-baseline-workspace')
    groups = [dict(name='diagnostic-oracle-to-wsl-64', direction='oracle-to-wsl',
                   size_mib=64, variants=list(variants), runs=1)]
elif args.phase == 'guards':
    groups = [dict(name=f'guard-{direction}-{size}', direction=direction, size_mib=size,
                   variants=list(variants), runs=10 if size < 1 else 5)
              for direction, size in (('oracle-to-wsl', 64), ('wsl-to-oracle', 512),
                                      ('wsl-to-oracle', 64), ('oracle-to-wsl', 0.0625),
                                      ('wsl-to-oracle', 0.0625))]
elif args.phase == 'overhead':
    variants = {
        'profile-off': dict(build, chunk_size=262144, completion_profile=False,
                            explicit_connection_close=False, endpoint_cwd=str(base),
                            build_id='completion-v1-profile-off'),
        'profile-on': dict(build, chunk_size=262144, completion_profile=True,
                           explicit_connection_close=False, endpoint_cwd=str(base),
                           build_id='completion-v1-profile-on'),
    }
    groups = [dict(name=f'overhead-s{series}-oracle-to-wsl-64', series=series,
                   direction='oracle-to-wsl', size_mib=64, variants=list(variants), runs=5)
              for series in (1, 2)]
else:
    groups = [dict(name=f's{series}-completion-oracle-to-wsl-512', series=series,
                   direction='oracle-to-wsl', size_mib=512, variants=list(variants), runs=5)]
name = 'completion-' + args.phase + '-20261004'
config = base / ('matrix-' + name + '.json')
assert not config.exists() and not (base / 'results' / name).exists()
config.write_text(json.dumps({'output_root': str(base / 'results' / name),
                              'variants': variants, 'groups': groups}, indent=2) + '\n')
print(config)
