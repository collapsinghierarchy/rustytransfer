"""Add untouched intermediate diagnostics provenance to an exported evidence set."""
import argparse
import json
import shutil
from pathlib import Path

BASE = Path('/home/wasilij/rustytransfer-bench')
parser = argparse.ArgumentParser()
parser.add_argument('destination', type=Path)
args = parser.parse_args()
assert args.destination.is_dir()
for source, relative in (
    (BASE / 'build-diagnostics-v2-20261003', Path('builds/diagnostics-v2-20261003')),
    (BASE / 'checks-diagnostics-20261003', Path('checks/diagnostics-20261003')),
):
    destination = args.destination / relative
    destination.mkdir(exist_ok=False)
    for path in sorted(source.iterdir()):
        if path.is_file() and (path.name == 'source.tar.gz' or path.suffix in ('.json', '.log', '.sarif')):
            shutil.copyfile(path, destination / path.name)
notes = {
    'diagnostics_v2': 'Unscored intermediate build. Scored refreshed diagnostics use v3; the original stale-build recovery note points to this intermediate step.',
    'initial_diagnostics_checks': 'Incomplete initial gate run: eight successful commands are recorded, without a completed cargo-deny/SARIF gate record. Superseded by the complete passing diagnostics-v3 and final shared-key checks.',
}
(args.destination / 'intermediate-records.json').write_text(json.dumps(notes, indent=2) + '\n')
target = args.destination / 'reproduction' / Path(__file__).name
assert not target.exists()
shutil.copyfile(Path(__file__), target)
print('Retained intermediate diagnostics build and incomplete initial checks')
