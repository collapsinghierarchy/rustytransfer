"""Compare frozen archive bytes with final workspace and commit provenance."""
import argparse
import difflib
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('manifest', type=Path)
p.add_argument('output', type=Path)
args = p.parse_args()
manifest = json.loads(args.manifest.read_text())
files = manifest['source_files_sha256']
archive = args.manifest.with_name('source.tar.gz')
assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest['source_archive_sha256']
changes=[]
patch=[]
with tarfile.open(archive, 'r:gz') as tar:
    assert set(tar.getnames()) == set(files)
    for name, expected in files.items():
        frozen = tar.extractfile(name).read()
        assert hashlib.sha256(frozen).hexdigest() == expected, name
        final=(REPO/name).read_bytes()
        if final!=frozen:
            changes.append(dict(path=name,frozen_sha256=expected,
                                final_sha256=hashlib.sha256(final).hexdigest()))
            patch.extend(difflib.unified_diff(frozen.decode().splitlines(keepends=True),
                                              final.decode().splitlines(keepends=True),
                                              fromfile='frozen-v1/'+name,tofile='final/'+name))
assert {item['path'] for item in changes}=={
    'crates/transfer/src/lib.rs','crates/transfer/src/session/sender.rs',
    'crates/transfer/src/session/receiver.rs','crates/native/src/transport/iroh.rs'}
patch_text=''.join(patch)
patch_path=args.output.with_name('post-measurement-lint-cleanup.patch')
args.output.parent.mkdir(parents=True,exist_ok=True)
patch_path.write_text(patch_text)
report = dict(frozen_file_count=len(files), all_archive_bytes_match_manifest=True,
              all_final_workspace_bytes_match_frozen=False,
              final_workspace_changes=changes,
              final_changes_scope='Private ProfileOptions groups the same two boolean flags; nested Iroh if-let collapsed. No performance result is attributed to this post-measurement lint cleanup.',
              final_changes_patch=patch_path.name,
              final_changes_patch_sha256=hashlib.sha256(patch_path.read_bytes()).hexdigest(),
              source_archive_sha256=manifest['source_archive_sha256'],
              frozen_source_commit=manifest['source_commit'],
              implementation_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                              cwd=REPO, text=True).strip(),
              source_dirty_at_freeze=manifest['source_dirty'],
              authority='Exact source archive and working-tree bytes; Git may normalize line endings.')
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report))
