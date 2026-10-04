"""Prepare planned cohort configs without running transfers."""
import argparse
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('phase', choices=['old-profile', 'diagnostics', 'chunks', 'chunks-screen', 'chunks-remaining', 'chunk-confirm-upload64', 'chunk-startup-retry', 'chunk-startup64-upload', 'shared-key-streams', 'shared-key-streams-confirm', 'copy', 'copy-screen', 'window-screen', 'profile-overhead'])
p.add_argument('--candidate', type=Path)
p.add_argument('--baseline', type=Path)
cli = p.parse_args()
base = Path('/home/wasilij/rustytransfer-bench')
baseline = json.loads((base / 'final-build-20261003.json').read_text())
if cli.baseline:
    baseline = json.loads(cli.baseline.read_text())
candidate = json.loads(cli.candidate.read_text()) if cli.candidate else baseline
variants = {}
groups = []
if cli.phase == 'old-profile':
    variants['baseline-profile'] = dict(baseline, profile=True, build_id='arm-current-baseline-profile')
    for direction in ('oracle-to-wsl', 'wsl-to-oracle'):
        groups.append(dict(name='refresh-' + direction, direction=direction, size_mib=512, variants=['baseline-profile'], runs=1))
elif cli.phase == 'diagnostics':
    variants['diagnostics'] = dict(candidate, profile=True, build_id='transport-diagnostics')
    for direction in ('oracle-to-wsl', 'wsl-to-oracle'):
        groups.append(dict(name='transport-' + direction, direction=direction, size_mib=512, variants=['diagnostics'], runs=1))
elif cli.phase in ('chunks', 'chunks-screen', 'chunks-remaining'):
    for chunk in (262144, 524288, 1048576):
        name = f'chunk-{chunk//1024}k'
        variants[name] = dict(candidate, chunk_size=chunk, build_id='chunk-sweep-diagnostics-source')
    for series in (1, 2):
        for direction in ('oracle-to-wsl', 'wsl-to-oracle'):
            for size in ((512, 64) if series == 1 else (64, 512)):
                groups.append(dict(name=f's{series}-{direction}-{size}', series=series, direction=direction, size_mib=size, variants=list(variants), runs=5))
    for direction, size in (('oracle-to-wsl', 0.0625), ('wsl-to-oracle', 0.00390625)):
        groups.append(dict(name='startup-' + direction, direction=direction, size_mib=size, variants=list(variants), runs=10))
    if cli.phase == 'chunks-screen':
        groups = groups[:1]
    elif cli.phase == 'chunks-remaining':
        groups = [group for group in groups if (group.get('series') == 1 and not (group['direction'] == 'oracle-to-wsl' and group['size_mib'] == 512)) or group['name'].startswith('startup-')]
        for spec in variants.values():
            spec['build_id'] = 'arm-current-baseline-chunk-sweep'
elif cli.phase == 'chunk-confirm-upload64':
    variants = {f'chunk-{chunk//1024}k': dict(candidate, chunk_size=chunk, build_id='arm-current-baseline-chunk-sweep') for chunk in (262144, 1048576)}
    groups = [dict(name='s2-wsl-to-oracle-64-confirm', series=2, direction='wsl-to-oracle', size_mib=64, variants=list(variants), runs=5)]
elif cli.phase in ('chunk-startup-retry', 'chunk-startup64-upload'):
    variants = {f'chunk-{chunk//1024}k': dict(candidate, chunk_size=chunk, build_id='arm-current-baseline-chunk-sweep') for chunk in (262144, 524288, 1048576)}
    size = 0.0625 if cli.phase == 'chunk-startup64-upload' else 0.00390625
    groups = [dict(name=cli.phase, direction='wsl-to-oracle', size_mib=size, variants=list(variants), runs=10)]
elif cli.phase in ('shared-key-streams', 'shared-key-streams-confirm'):
    assert candidate['artifact_kind'] == 'example:shared_key_parallel'
    variants = {f'streams-{count}': dict(candidate, chunk_size=262144, experimental_streams=count, build_id='shared-key-parallel-example') for count in (1, 4)}
    series = 2 if cli.phase.endswith('-confirm') else 1
    groups = [dict(name=f's{series}-shared-key-oracle-to-wsl-512', series=series, direction='oracle-to-wsl', size_mib=512, variants=list(variants), runs=5)]
elif cli.phase == 'profile-overhead':
    variants = {'standard': dict(candidate, profile=False, build_id='diagnostic-overhead-control'), 'profile': dict(candidate, profile=True, build_id='diagnostic-overhead-profile')}
    groups = [dict(name=f's{series}-profile-overhead-64', series=series, direction='oracle-to-wsl', size_mib=64, variants=list(variants), runs=5) for series in (1, 2)]
elif cli.phase == 'window-screen':
    for window in (None, 2_500_000, 5_000_000):
        name = 'default' if window is None else f'window-{window//1000}k'
        variants[name] = dict(candidate, stream_window_bytes=window, build_id='stream-window-' + name)
    groups = [dict(name='s1-window-oracle-to-wsl-512', direction='oracle-to-wsl', size_mib=512, variants=list(variants), runs=5)]
elif cli.phase in ('copy', 'copy-screen'):
    variants = {'baseline': dict(baseline, build_id='owned-chunk-baseline'), 'candidate': dict(candidate, build_id='owned-chunk-candidate')}
    for series in (1, 2):
        for direction in ('oracle-to-wsl', 'wsl-to-oracle'):
            for size in (64, 512):
                groups.append(dict(name=f's{series}-{direction}-{size}', series=series, direction=direction, size_mib=size, variants=list(variants), runs=5))
    for direction, size in (('oracle-to-wsl', 0.0625), ('wsl-to-oracle', 0.00390625)):
        groups.append(dict(name='startup-' + direction, direction=direction, size_mib=size, variants=list(variants), runs=10))
    if cli.phase == 'copy-screen':
        groups = [group for group in groups if group['direction'] == 'oracle-to-wsl' and group['size_mib'] == 512]
path = base / ('matrix-' + cli.phase + '.json')
assert not path.exists()
path.write_text(json.dumps({'output_root': str(base / 'results' / ('next-' + cli.phase + '-20261003')), 'variants': variants, 'groups': groups}, indent=2) + '\n')
print(path)
