# Repository publication

Before publishing repository changes through Git or a GitHub API, run the maintained Betterleaks guard on the full object ID of every revision being published:

```sh
sh scripts/check-betterleaks.sh FULL_OBJECT_ID
```

The installed pre-push hook performs this check automatically. In a new clone, install the guard as described in [scripts/PUSH_GUARD.md](scripts/PUSH_GUARD.md). The installed dispatcher also protects linked worktrees.

If Betterleaks reports findings, warnings, errors, or an incomplete scan, do not publish. Do not bypass the guard with `--no-verify`, alternate hook paths, scanner overrides that weaken detection, or API writes. Do not automatically suppress findings or rewrite existing history to make the check pass. Fix or review the findings within the user's authorized scope, then rerun the check. CI runs after publication and cannot replace the local check.

Keep reports redacted and local. Report counts, rules, and paths when needed; never print credential values.

The push-guard tests may seed and push to disposable local bare repositories to verify hook behavior. That test-only exception does not authorize publication to a network remote.
