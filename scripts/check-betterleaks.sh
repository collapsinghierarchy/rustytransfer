#!/bin/sh
# Shared by pre-push and CI. No automatic baseline or suppression is applied.
set -eu
export GIT_NO_REPLACE_OBJECTS=1
guard_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(git rev-parse --show-toplevel)
cd "$root"
reference=${1:-$(git rev-parse HEAD)}
case "$reference" in *[!0-9a-fA-F]*|'') echo 'Betterleaks blocked: a full commit or tag object ID is required.' >&2; exit 1 ;; esac
case "${#reference}" in 40|64) ;; *) echo 'Betterleaks blocked: abbreviated object IDs are not supported.' >&2; exit 1 ;; esac
if [ "$(git rev-parse --is-shallow-repository)" != false ]; then
    echo 'Betterleaks blocked: fetch the complete Git history before scanning.' >&2
    exit 1
fi
git cat-file -e "$reference^{commit}" || { echo 'Betterleaks blocked: pushed object does not resolve to a commit.' >&2; exit 1; }
if [ -n "${BETTERLEAKS_CONFIG:-}${GITLEAKS_CONFIG:-}${BETTERLEAKS_CONFIG_TOML:-}${GITLEAKS_CONFIG_TOML:-}" ]; then
    echo 'Betterleaks blocked: scanner configuration overrides require an explicit reviewed policy change.' >&2
    exit 1
fi
for config in .betterleaks.toml .gitleaks.toml .betterleaksignore .gitleaksignore; do
    if [ -e "$root/$config" ]; then
        echo "Betterleaks blocked: $config requires an explicit reviewed policy change." >&2
        exit 1
    fi
done
version=$(tr -d '\r\n' < "$guard_dir/betterleaks-version.txt")
scanner=${BETTERLEAKS_BIN:-}
if [ -z "$scanner" ]; then
    if command -v betterleaks >/dev/null 2>&1; then
        scanner=$(command -v betterleaks)
    else
        case "$(uname -s)" in
            MINGW*|MSYS*|CYGWIN*)
                case "$(uname -m)" in aarch64|arm64) arch=arm64 ;; *) arch=x64 ;; esac
                scanner="$guard_dir/../target/security-tools/betterleaks-$version-windows-$arch/betterleaks.exe"
                ;;
            *) echo 'Betterleaks blocked: install the pinned scanner and put it on PATH (see scripts/PUSH_GUARD.md).' >&2; exit 1 ;;
        esac
    fi
fi
if [ ! -x "$scanner" ]; then
    echo 'Betterleaks blocked: the scanner is missing or not executable.' >&2
    exit 1
fi
actual_version=$("$scanner" --version </dev/null 2>&1) || { echo 'Betterleaks blocked: cannot determine scanner version.' >&2; exit 1; }
case "$actual_version" in
    "betterleaks version $version"|"betterleaks version v$version"|"$version"|"v$version") ;;
    *) echo "Betterleaks blocked: scanner must be version $version." >&2; exit 1 ;;
esac
perl -MJSON::PP -e 1 || { echo 'Betterleaks blocked: Perl JSON::PP is required to verify the report.' >&2; exit 1; }
mkdir -p "$root/target/security-checks"
run_dir=$(mktemp -d "$root/target/security-checks/scan.XXXXXX")
echo "Betterleaks reports: $run_dir" >&2

run_scan() {
    mode=$1
    source_path=$2
    report=$3
    log=$4
    shift 4
    status=0
    "$scanner" "$mode" "$source_path" \
        --report-path "$report" --report-format sarif \
        --redact=100 --no-color --no-banner --log-level info \
        --ignore-gitleaks-allow --exit-code 1 --timeout 300 \
        "$@" </dev/null >"$log" 2>&1 || status=$?
    # Redacted scanner output stays available even when the command fails.
    cat "$log" >&2
    if [ "$status" -ne 0 ]; then
        echo "Betterleaks blocked: scanner exited with status $status." >&2
        return 1
    fi
    if grep -Eiq '(^|[[:space:]])(WRN|ERR|FTL|PNC|WARN(ING)?|ERROR|FATAL)([[:space:]:]|$)' "$log"; then
        echo 'Betterleaks blocked: scanner emitted a warning or error.' >&2
        return 1
    fi
    if ! grep -Eq '(^|[[:space:]])INF[[:space:]].*no leaks found([[:space:]]|$)' "$log"; then
        echo 'Betterleaks blocked: successful scan completion was not confirmed.' >&2
        return 1
    fi
    perl "$guard_dir/validate-betterleaks-report.pl" "$report" || return 1
}

git_status=0
run_scan git "$root" "$run_dir/history.sarif" "$run_dir/history.log" \
    "--log-opts=--full-history --diff-merges=first-parent $reference" || git_status=$?
# CI keeps its existing SARIF upload location, including failed scan evidence.
if [ -n "${2:-}" ] && [ -s "$run_dir/history.sarif" ]; then
    mkdir -p "$(dirname -- "$2")"
    cp "$run_dir/history.sarif" "$2"
fi
if [ "$git_status" -ne 0 ]; then exit 1; fi

# Git log does not scan annotated tag messages. Check every tag in the chain.
object=$reference
tag_count=0
while [ "$(git cat-file -t "$object")" = tag ]; do
    tag_count=$((tag_count + 1))
    if [ "$tag_count" -gt 64 ]; then echo 'Betterleaks blocked: tag chain exceeds the supported depth.' >&2; exit 1; fi
    mkdir -p "$run_dir/tags"
    git cat-file tag "$object" > "$run_dir/tags/$object.txt"
    object=$(sed -n '1s/^object //p' "$run_dir/tags/$object.txt")
done
if [ "$tag_count" -gt 0 ]; then
    run_scan dir "$run_dir/tags" "$run_dir/tags.sarif" "$run_dir/tags.log" || exit 1
fi
echo 'Betterleaks passed: complete scan, no findings, warnings, or errors.' >&2
