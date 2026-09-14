# Alert lifecycle trials

This document is the executable plan for checking Clippy alert identity on the
default branch. The harness is read-only: it uses `gh api` only with GET and
never dismisses, restores, deletes, uploads, commits, or pushes anything.

The suite tests the exact scope `(full ref, tool, category)` and the exact
commit that was analyzed. A pull request run normally belongs to a merge ref,
so these default-branch lifecycle trials run one fixture at a time on
`refs/heads/main`. Each fixture is pushed, verified after its own completed CI
run, reverted in a separate push, and verified again before the next fixture.

## Worker contract

The CI worker retains the exact `target/clippy.sarif` produced immediately
before upload. It also records the workflow `run_id`, `run_attempt`, and the
SARIF upload id when the upload path exposes one. A suggested artifact layout
is:

```text
trial-artifacts/<trial>/<commit-sha>/<run-id>/clippy.sarif
trial-artifacts/<trial>/<commit-sha>/<run-id>/clippy.json
trial-artifacts/<trial>/<commit-sha>/<run-id>/clippy-upload-metadata.json
```

`clippy-upload-metadata.json` is the worker's exact metadata contract and
contains the following fields (plus completion/outcome and tool version
details):

```json
{
  "schema_version": 1,
  "artifact_name": "clippy-sarif-123456789-1",
  "run_id": "123456789",
  "run_attempt": "1",
  "sarif_id": "upload-id-if-exposed",
  "commit_sha": "exact-40-character-sha",
  "ref": "refs/heads/main",
  "tool": "clippy",
  "category": "Code Scanner",
  "sarif_id": "upload-id-if-exposed",
  "report_complete": "true",
  "clippy_step_outcome": "success",
  "upload_step_outcome": "success",
  "configured_floor": "150",
  "files": {"raw_clippy_json": "target/clippy.json",
             "uploaded_clippy_sarif": "target/clippy.sarif"},
  "versions": {"rustc": "...", "clippy_sarif": "..."}
}
```

The harness requires these exact snake_case keys. It does not silently accept
aliases such as `commitSHA` or `attempt`.

The worker passes the exact values to the harness. The SARIF file is not
reconstructed from source or from an alert page.

For a completed positive run:

```bash
LC=.github/scripts/alert-lifecycle.py

python3 "$LC" snapshot \
  --repo collapsinghierarchy/rustytransfer \
  --ref refs/heads/main \
  --commit-sha "$COMMIT_SHA" \
  --tool clippy \
  --category 'Code Scanner' \
  --sarif-id "$SARIF_ID" \
  --run-id "$RUN_ID" \
  --run-attempt "$RUN_ATTEMPT" \
  --metadata "$ARTIFACT/clippy-upload-metadata.json" \
  --uploaded-sarif "$ARTIFACT/clippy.sarif" \
  --previous "$BASELINE" \
  --out "$SNAPSHOT"
```

`--previous` requires a different analysis id and, when available, a
different SARIF upload id. The command polls the SARIF upload until complete,
then selects exactly one analysis whose `ref`, `tool`, `category`, and
`commit_sha` all match. It never uses the newest analysis as a fallback. The
`run_id` check verifies `head_sha`, completion, and the exposed
`run_attempt`.

T0 additionally requires the retained artifact:

```bash
python3 "$LC" snapshot ... \
  --metadata "$ARTIFACT/clippy-upload-metadata.json" \
  --uploaded-sarif "$ARTIFACT/clippy.sarif" \
  --require-uploaded-artifact \
  --out t0-baseline.json
```

The downloaded SARIF must contain
`partialFingerprints.primaryLocationLineHash` and
`properties["github/alertNumber"]` for every result. The harness compares the
uploaded and downloaded documents by SARIF run/result index, rule id, artifact
URI, and supplied fingerprint. Missing enrichment or any mismatch fails with
the exact missing field or mismatch; no source span or line proximity is
guessed.

For a failed compile or invalid-gate run:

```bash
python3 "$LC" negative \
  --baseline "$BASELINE" \
  --failure-sha "$COMMIT_SHA" \
  --run-id "$RUN_ID" \
  --run-attempt "$RUN_ATTEMPT" \
  --metadata "$ARTIFACT/clippy-upload-metadata.json" \
  --out "$SNAPSHOT"
```

This requires the exact `clippy` job's `Upload Clippy SARIF report` step to be
`completed/skipped`, finds no comparable Clippy analysis at the failure SHA,
and compares all baseline alert states. A skipped upload is not inferred from
the absence of a current SARIF result.

Compare with a trial-specific cohort expectation:

```bash
python3 "$LC" compare \
  --baseline "$BASELINE" \
  --current "$SNAPSHOT" \
  --expect expectations/T1.json \
  --out "$SNAPSHOT.diff.json"
```

An expectation names affected alert numbers or selectors such as actual rule
ids, paths, and the selected doc-lint cohort. All records outside that cohort
must remain unchanged. Expected changes are expressed as fields, for example:

```json
{
  "trial": "T1",
  "affected": [
    {"alert_number": 101,
     "expect": {"state": "dismissed", "fingerprint": "same", "path": "same"}},
    {"alert_number": 102,
     "expect": {"state": "dismissed", "fingerprint": "same", "path": "same"}}
  ]
}
```

The normalized diff retains alert number, state, rule id, path, fingerprint,
dismissal reason/comment, and a `fixed_at_present` boolean. It excludes capture
timestamps, analysis ids, and source line/column coordinates. Raw API and SARIF
evidence, including coordinates and the original `fixed_at`, remains in the
snapshot for audit. Alert numbers are
observed server identifiers; the harness does not claim that a matching tuple
guarantees GitHub will reuse one.

For a restoration comparison, use the restore oracle instead of demanding
global equality with all historical records:

```json
{
  "trial": "T1-restore",
  "restore": {
    "active_baseline": "unchanged",
    "allowed_fixed_history": [1201, 1202]
  }
}
```

Every baseline alert must still exist with the same stable identity and state.
Only the explicitly listed, newly created fixed alert numbers may be
current-only records. An unlisted fixed alert, a missing baseline alert, or a
changed baseline alert fails. Closed history remains visible and checked; it
is never silently discarded. A fixed historical record must carry
`fixed_at` evidence; `state: dismissed` with `fixed_at` is accepted as a fixed
historical record, while `state` alone is never treated as a fix.

Repeated fingerprints are stored as a list of alert numbers. A repeated
fingerprint for the same alert number is an explicit collision failure. The
harness does not redesign the scanner's duplicate policy.

## Trial order

Before T0, merge the identity/workflow correction that is under test and push
it to `main`. Check that no workflow run for the target ref is queued or in
progress. Record the pre-suite source SHA and the current state, reason, and
comment of every alert that a trial may touch.

| Trial | Mutation and assertion |
| --- | --- |
| T0 | Verify the Clippy `Code Scanner` scope starts empty, or stop and use a fresh controlled scope. Leave OSV analyses and alerts untouched. Run one successful report and save the retained-artifact-validated baseline. Never delete report history to manufacture emptiness. |
| T1 | Choose three open alerts with different shapes, including the WebRTC indexing warning that will be T3's fix target. Record their prior states, then dismiss them manually with the audit comment `Alert lifecycle trial T1: temporary dismissal; restore after verification.` Take the comparison baseline after those dismissals. Apply formatting-only changes, verify the three alert numbers, fingerprints, and dismissal fields remain stable, revert T1, verify the dismissed state again, then immediately restore the three original states. Save the post-undismiss snapshot as the next baseline. |
| T2 | Select the actual doc-lint cohort from the T0 SARIF by rule id, rather than assuming a count. Edit only the bodies of the documented items. Require the cohort's alert numbers/fingerprints/states and every outside record to remain stable. Restore the bodies. |
| T3 | Fix exactly the selected WebRTC indexing warning, now open because T1's dismissal was restored. Require its old alert to carry `fixed_at` evidence (even if API state remains `dismissed`) or be absent, while the outside cohort is unchanged. Restore the warning and record whether GitHub reopens the prior alert or creates a new one; do not assume either outcome. |
| T4 | Run independent formatting/comment/whitespace changes that should preserve identity, then an independent literal-content change that should change the affected identity. Require the first to be stable and the second to be an allowed rekey. Restore after each subtrial. |
| T5 | While the duplicate fixture is present, triage and clean up only the fixture's own alerts and record their states; do not reopen fixed ghost alerts from prior experiments. Independently delete, insert, and reorder the first member of a duplicate set. Use `"mode": "stateless-limitation"` and select the duplicate cohort explicitly. Preserve every observed id, fingerprint, state, and evidence location. A rekey or wrong dismissal is reported as `LIMITATION/FAIL` with exit code 3; it is never silently reported as PASS. If no rekey is observed, the trial fails because it was not exercised. Revert the fixture seed after each mutation. |
| T6 | Introduce one deliberate compile error. The negative command must find the upload step `completed/skipped`, no comparable analysis at that failure SHA, and an unchanged baseline state for every alert. Fix the error and verify the restoration run. |
| T7 | Run three separate gate mutations: a valid count above the floor, malformed `CLIPPY_MIN_RESULTS=17o`, and an overflowing digits-only value. The valid case requires a new completed analysis. Each invalid case requires skipped upload, no analysis at that SHA, and unchanged alert states. Restore the gate setting after each run. |
| T8 | Rename one source file while keeping the project compilable and the target membership unchanged. Measure old-path fixed alerts and new-path open alerts with an allowed rekey cohort. This quantifies path cost; it does not assert that a path rename preserves identity. Restore the original path. |

For T3, the affected entry should assert `fixed_at_present: true` and may set
`allow_absent: true` when GitHub removes the old alert record entirely:

```json
{
  "trial": "T3-webrtc-index-fix",
  "affected": [
    {"alert_number": 123, "allow_absent": true,
     "expect": {"fixed_at_present": true, "fingerprint": "same"}}
  ]
}
```

For T5, a suitable expectation starts like this:

```json
{
  "trial": "T5-delete-first-duplicate",
  "mode": "stateless-limitation",
  "affected": [{"selector": {"path": "src/example.rs", "rule_id": "clippy::unwrap_used"}}]
}
```

The comparison output includes changed records, additions, removals,
same-base-fingerprint rekeys, and possible dismissed-to-open rekeys. This is
the expected limitation of a stateless ordinal; the raw evidence is what lets
the operator inspect whether a dismissal landed on the wrong occurrence.

## Controls and cleanup

Every push must use one fixture and one run. Wait for that run and its Clippy
analysis before pushing the restoration. A restoration is itself a new push
and requires its own positive snapshot or negative verification. Capture the
run id and run attempt from that exact run; retries create a different attempt
and must not be mistaken for the previous attempt. Refuse to proceed while
another run for `refs/heads/main` is queued or in progress.

At the end, restore only the alerts touched by T1 and any other trial that
changed a dismissal. Restore each original state, reason, and comment, and
verify the final state through the alert API. T5 cleanup happens while its
fixture is present and is limited to that fixture; do not reopen fixed ghost
alerts created by earlier trials. Do not delete analyses or alerts to hide a
failed transition. The final source tree must equal the tested baseline, apart
from the permanent workflow/harness corrections accepted for the project; all
trial fixtures must be reverted.

The suite is complete only when the final restoration run is complete and its
restore-oracle comparison passes: active baseline identities and states match,
and every additional fixed historical alert is explicitly listed. The OSV
scope is unchanged and is never used as evidence for a Clippy result.
