# Betterleaks before every push

The tracked `.githooks/pre-push` hook runs `scripts/check-betterleaks.sh` before Git sends updates. The Betterleaks CI job uses that same check and still uploads its SARIF evidence on failure.

Every pushed branch or tag is checked using its actual object ID, including pushes of branches other than HEAD and multiple-ref pushes. The scan covers the complete reachable history and merge-resolution changes, not just the current files or the latest diff. Annotated tag messages are checked separately. Ref deletions send no new objects and do not require a scan.

The check requires pinned Betterleaks 1.8.1, a complete Git history, and Perl's core `JSON::PP` module (provided by Git for Windows and the Ubuntu CI environment). It blocks on findings, any scanner warning/error, nonzero exit, missing completion confirmation, invalid/missing SARIF, or failed SARIF invocations. Missing tools or shallow clones block pushes too. Output and reports are redacted and retained under the ignored `target/security-checks/` directory.

## Enable in this clone

On Windows, the installer downloads the pinned native release if needed, checks its published SHA-256 checksum, and installs a dispatcher in the shared Git hooks directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-push-guard.ps1
```

This changes script execution policy only for that process. The installer refuses to replace an existing custom hook or hook path. The native scanner stays under `target/security-tools/`; no system PATH or Git hook-path configuration is changed. The dispatcher covers linked worktrees too, resolving the maintained guard from the primary checkout. If that checkout no longer contains the guard, pushes fail closed.

On Linux/macOS, install the pinned scanner on PATH and enable the hook from the repository root:

```sh
version=$(cat scripts/betterleaks-version.txt)
go install -ldflags="-X github.com/betterleaks/betterleaks/version.Version=$version" \
  "github.com/betterleaks/betterleaks@v$version"
export PATH="$(go env GOPATH)/bin:$PATH"
# Integrate any existing custom hooks or core.hooksPath before installing.
# This location is shared by linked worktrees; do not overwrite an existing hook.
hook=$(git rev-parse --git-path hooks/pre-push)
test ! -e "$hook" || { echo 'An existing hook needs integration'; exit 1; }
cp .githooks/dispatch-pre-push "$hook"
chmod +x "$hook"
```

Run a manual check in Git Bash or a POSIX shell:

```sh
sh scripts/check-betterleaks.sh
# Equivalent check for one specific committed revision:
sh scripts/check-betterleaks.sh "$(git rev-parse HEAD)"
```

`BETTERLEAKS_BIN` can select a scanner executable; its version must still match the pin. Default scanner configuration and ignore-file overrides are refused, and inline allow comments are disabled. No automatic allowlist or baseline makes old findings disappear. Fixing a file in a new commit does not remove a finding from its earlier history. Historical findings need review; history rewriting or a reviewed exception policy is a separate change. Do not publish report contents containing credentials; the maintained commands use full redaction and no live credential validation.

## Limits and validation

This is a local safeguard. Git can bypass hooks, and fresh clones do not enable them automatically. CI checks happen after upload, so CI alone cannot stop a secret reaching the remote. Require the Betterleaks check for merges as a separate repository policy; the hook installation does not change GitHub branch rules or protect pushes from another clone. Agents using this clone must keep the hook enabled and must not bypass a failed check.

The guard is tested against temporary local bare repositories; no GitHub push is used:

```sh
PUSH_GUARD_REAL_SCANNER="$(command -v betterleaks)" \
  python3 -m unittest discover -s scripts/tests -p 'test_push_guard*.py' -v
```

All 23 tests passed locally on 2026-10-05: 18 tests of failure/ref/report handling and five tests using the real pinned Linux scanner. Clean pushes, historical generated-token findings, annotated tag messages, and linked worktrees were exercised against disposable local bare remotes. The installed Windows dispatcher also rejected this repository's real history. The PowerShell installer is idempotent and its dispatcher is executable in WSL. Workflow YAML was parsed and all other CI jobs were verified unchanged. CI itself has not run this change remotely.

The initial local history scan on 2026-10-05 reproduced 76 findings (73 generic API-key findings and three generic-password findings), including historical benchmark provenance and an archived helper. Findings remain blocked pending review. These counts do not establish whether a credential is live, and no findings were automatically suppressed or published to a remote. Tests are synthetic and do not validate credentials against live APIs. If `PUSH_GUARD_REAL_SCANNER` is unset, only the 18 scanner-independent tests run; CI sets it explicitly after installing Betterleaks.
