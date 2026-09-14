#!/usr/bin/env python3
"""Carry code scanning dismissals across a fingerprint-scheme change.

Changing ``FINGERPRINT_VERSION`` is a one-way door. GitHub closes every alert
whose ``primaryLocationLineHash`` changed and opens a new, unlinked alert in its
place; dismissal state ("won't fix", "false positive", "used in tests") is not
inherited, and the REST API does not expose fingerprints, so the new alerts
cannot be matched back to the old ones by identity.

The only reliable migration is therefore out of band:

    1. BEFORE the cutover   ``export``  - snapshot every dismissed alert
    2. push the new scheme, let one analysis complete on the default branch
    3. AFTER the cutover    ``apply``   - re-dismiss the matching new alerts

The cutover this script was written for is ``rustytransfer-clippy-v2`` ->
``rustytransfer-clippy-v5``; v2 is the scheme whose fingerprints are live on
GitHub today. No v3 or v4 was ever deployed, so "the old scheme" means v2
throughout.

Two properties of v2 shape what ``apply`` can promise:

  * v2 was **not** collision-free - the current report yields 174 distinct v2
    fingerprints for 177 results. GitHub raises one alert per distinct
    fingerprint, so a single dismissed v2 alert can stand for several v5
    findings. Such a record resolves to more than one open alert and is reported
    AMBIGUOUS for hand review; that is not a defect in the matcher, it is the v2
    collision showing through.
  * a v2-era snapshot was written by whatever revision of this script existed
    when it was taken. If its records carry an identity of a different shape than
    the one :func:`location_identity` builds today, the two cannot be compared at
    all - and the snapshot cannot be retaken, because the alerts it describes
    stopped existing at the cutover. Those records are reported UNRESOLVED with
    the old alert's URL and must be re-dismissed by hand. They are never matched
    automatically, not even under ``--allow-fuzzy``: with no identity to
    corroborate, all that is left is Clippy's generic message text plus a line
    number, and a line number is exactly what an insertion above the finding
    invalidates.

Matching is by reconstructed identity: the same tiers the fingerprint is built
from - the enclosing item chain, the normalized diagnostic span and the enclosing
statement - recomputed from the checkout the alerts point at. Each open alert can
be claimed by at most one saved record, so two records can never dismiss the same
alert and lose one of the dismissals. Anything that does not resolve to exactly
one unclaimed alert is reported and skipped.

The weaker (rule id, path, message) fallback, with the recorded line as a
tie-breaker, is opt-in behind ``--allow-fuzzy`` and **can dismiss the wrong
alert**: Clippy's message text is generic enough that several open alerts match
it, and the nearest-line tie-break prefers whatever now occupies the line the
dismissed finding used to be on. It is therefore corroborated by the enclosing
item chain - a candidate in a different item is rejected outright - and is only
offered for records that do carry a comparable identity. Files renamed between
the two runs are not followed across the rename in either mode.

Requires GITHUB_TOKEN (or GH_TOKEN) with the ``security_events`` scope.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from rustscan import enclosing_context, item_chain, masks, scan_items  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "stabilizer", SCRIPTS / "stabilize-clippy-sarif.py"
)
_stabilizer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stabilizer)
normalize_span = _stabilizer.normalize_span
decode = _stabilizer.decode

API = "https://api.github.com"

_SOURCE_CACHE: dict = {}


def line_starts(text: str):
    r"""Byte offset of the first byte of every line, splitting only on ``"\n"``.

    ``str.splitlines`` is wrong here. :func:`decode` maps bytes to characters one
    for one (latin-1), so after decoding, a form feed, a ``\x85`` or a ``\x1c``
    in the file is just an ordinary byte - ``\x85`` is a continuation byte of a
    great many UTF-8 characters - yet ``splitlines`` treats each of them as a
    line break. rustc, which produced the line numbers being reconstructed here,
    counts only ``\n``. Splitting on anything else shifts every line number below
    such a byte, and the reconstruction then anchors to unrelated code.
    """
    starts = [0]
    index = text.find("\n")
    while index != -1:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts


def _line_text(text: str, starts, line_number: int) -> str:
    """The bytes of one 1-based line, trailing newline included."""
    begin = starts[line_number - 1]
    end = starts[line_number] if line_number < len(starts) else len(text)
    return text[begin:end]


def column_offset(line: str, column) -> int:
    """Byte offset within ``line`` of the 1-based *character* column ``column``.

    rustc reports columns counted in characters and clippy-sarif passes them
    through, so the code scanning REST API hands back character columns, while
    every offset in the identity pipeline is a byte offset - the source is
    decoded latin-1 precisely so that string index == byte offset. On an ASCII
    line the two numbers are the same. On a line that is not they are not:
    ``src/bin/rustytransferCLI.rs`` carries a U+2026 ellipsis on five lines,
    three bytes wide and one column wide, so naive subtraction lands two bytes
    short for every preceding non-ASCII character - enough to cut a token in half
    and anchor the finding to a different statement.

    So the line is re-encoded to the bytes it came from, decoded as UTF-8, the
    column applied in characters, and the result measured back in UTF-8 bytes. A
    pure-ASCII line, or one that is not valid UTF-8, takes the column as a byte
    count, which for those lines is exact.
    """
    index = max(0, (column or 1) - 1)
    if index == 0:
        return 0
    if line.isascii():
        return min(index, len(line))
    try:
        raw = line.encode("latin-1")
        decoded = raw.decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return min(index, len(line))
    return min(len(decoded[:index].encode("utf-8")), len(line))


def source_facts(repository: Path, path: str):
    """(text, items, code_mask, comment_mask, line_starts) for a repo-relative path.

    ``None`` when the file is not in the checkout. Both masks are needed: the
    span tier drops comment bytes, and the context tier walks delimiters, which
    is only correct over real code.
    """
    if path not in _SOURCE_CACHE:
        try:
            text = decode((repository / path).read_bytes())
        except OSError:
            _SOURCE_CACHE[path] = None
        else:
            code, comments = masks(text)
            _SOURCE_CACHE[path] = (
                text,
                scan_items(text),
                code,
                comments,
                line_starts(text),
            )
    return _SOURCE_CACHE[path]


def location_identity(repository: Path, rule_id: str, path: str, location: dict):
    """Rebuild a finding's identity (minus the ordinal) from an alert's location.

    The REST API never exposes fingerprints, so an alert can only be tied back to
    a finding by recomputing its identity from the source it points at. All three
    tiers the fingerprint is built from are rebuilt - chain, span and context -
    because an identity missing a tier is strictly weaker than the one it stands
    in for: it would match alerts the fingerprint itself tells apart, and the
    whole point of matching on identity is that it is more selective than the
    message text.

    Columns are 1-based *character* offsets and are converted to byte offsets by
    :func:`column_offset`, so a non-ASCII source reconstructs byte-exactly
    instead of a few bytes short.

    An alert can also point outside the source it names - that is what every
    alert whose file shrank since the analysed commit looks like, and shrinking
    is routine in the documented workflow. Such an alert is *unresolved*, not
    anchored to whatever byte happens to be nearest: a start line past the end of
    the file, or a start column past the end of its own line, returns ``None``.
    Only the extent is clamped, so a span that merely runs off the end of the
    file still anchors at its start. Nothing here raises on a malformed location,
    and :func:`alert_identity` catches anything unforeseen that does, because one
    bad alert must not abort a migration.
    """
    facts = source_facts(repository, path or "")
    if facts is None or not location:
        return None
    text, items, code, comments, starts = facts
    limit = len(text)

    start_line = location.get("start_line")
    if not start_line or start_line < 1 or start_line > len(starts):
        return None
    start_text = _line_text(text, starts, start_line)
    if max(0, (location.get("start_column") or 1) - 1) > len(start_text.rstrip("\r\n")):
        # Past the end of its own line: this alert was analysed against a longer
        # version of this file. Degrade it to unresolved rather than anchor it to
        # an unrelated byte - a wrong anchor can match a saved record and dismiss
        # the wrong alert, an unresolved one only asks for a hand review.
        return None
    end_line = location.get("end_line") or start_line
    end_line = min(max(end_line, start_line), len(starts))

    def offset_of(line_number, column):
        base = starts[line_number - 1]
        line = _line_text(text, starts, line_number)
        return min(base + column_offset(line, column), limit)

    start = offset_of(start_line, location.get("start_column"))
    end = offset_of(end_line, location.get("end_column"))
    if end <= start:
        end = min(start + len(start_text.rstrip("\r\n")), limit)

    return (
        rule_id,
        path,
        item_chain(items, start),
        normalize_span(text[start:end], comments, start),
        enclosing_context(text, code, items, start, comments),
    )


def alert_identity(repository: Path, rule_id: str, path: str, location: dict):
    """:func:`location_identity` with every failure degraded to ``None``.

    A migration walks every open alert in the repository, and an alert can be
    malformed in ways no amount of clamping anticipates - a location no scanner
    would produce, a path that is not the Rust the scanner assumes, an
    unforeseen rustscan edge case. Letting one of those raise is the worst
    outcome on offer: it aborts the run with a traceback and loses the whole
    migration, including every record that matched cleanly, over a single alert.
    Degrading instead costs exactly one record a hand review, and both commands
    report it rather than passing it off as benign.
    """
    try:
        return location_identity(repository, rule_id, path, location)
    except Exception as error:  # noqa: BLE001 - deliberate: never abort the run
        print(
            f"  warning: could not anchor {rule_id} {path}: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return None


# rule id, path, chain, span, context - the shape location_identity returns. A
# snapshot taken by an older revision of this script carries a differently shaped
# tuple; comparing it against a current one would silently never match, so it is
# rejected as unresolved instead. It cannot be repaired after a cutover - see the
# module docstring - so such records go to hand review, never to a guess.
IDENTITY_ARITY = 5


def token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    print("error: set GITHUB_TOKEN (or GH_TOKEN) with the security_events scope", file=sys.stderr)
    raise SystemExit(2)


def request(method: str, url: str, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8")), response.headers
        except urllib.error.HTTPError as error:
            # Secondary rate limits are transient; 4xx other than 403/429 are not.
            if error.code in (403, 429) and attempt < 4:
                time.sleep(2 ** attempt * 5)
                continue
            detail = error.read().decode("utf-8", errors="replace")[:400]
            print(f"error: {method} {url} -> {error.code} {detail}", file=sys.stderr)
            raise SystemExit(1)
    raise SystemExit(1)


def paginate(repo: str, params: dict):
    url = f"{API}/repos/{repo}/code-scanning/alerts?" + urllib.parse.urlencode(
        {**params, "per_page": 100}
    )
    while url:
        page, headers = request("GET", url)
        yield from page
        url = None
        for part in (headers.get("Link") or "").split(","):
            if 'rel="next"' in part:
                url = part[part.find("<") + 1 : part.find(">")]
    return


def key_of(alert: dict):
    instance = alert.get("most_recent_instance") or {}
    location = instance.get("location") or {}
    return (
        (alert.get("rule") or {}).get("id"),
        location.get("path"),
        ((instance.get("message") or {}).get("text") or "").strip(),
    )


def describe(alert: dict) -> str:
    """``#12 clippy::foo src/lib.rs:7  <url>`` - enough for a human to act on it."""
    instance = alert.get("most_recent_instance") or {}
    location = instance.get("location") or {}
    return (
        f"#{alert.get('number')} {(alert.get('rule') or {}).get('id')} "
        f"{location.get('path')}:{location.get('start_line')}  "
        f"{alert.get('html_url') or '<no url>'}"
    )


def cmd_export(args) -> int:
    alerts = [
        a
        for a in paginate(args.repo, {"tool_name": args.tool, "state": "dismissed"})
    ]
    records = []
    unresolved = 0
    for alert in alerts:
        instance = alert.get("most_recent_instance") or {}
        location = instance.get("location") or {}
        rule_id = (alert.get("rule") or {}).get("id")
        path = location.get("path")
        identity = alert_identity(args.repository, rule_id, path, location)
        if identity is None:
            unresolved += 1
        records.append(
            {
                "number": alert.get("number"),
                "rule_id": rule_id,
                "path": path,
                "start_line": location.get("start_line"),
                "start_column": location.get("start_column"),
                "message": ((instance.get("message") or {}).get("text") or "").strip(),
                "identity": list(identity) if identity else None,
                "dismissed_reason": alert.get("dismissed_reason"),
                "dismissed_comment": alert.get("dismissed_comment"),
                "category": instance.get("category"),
                "html_url": alert.get("html_url"),
            }
        )
    args.out.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(records)} dismissed alert(s) to {args.out}")
    if unresolved:
        print(f"  warning: {unresolved} could not be anchored to source; on re-apply "
              f"they are reported as UNRESOLVED and have to be re-dismissed by hand. "
              f"If that count is a surprise, check that --repository is the checkout "
              f"the analysis ran on - and fix it before the cutover, because after "
              f"the cutover the snapshot cannot be retaken.",
              file=sys.stderr)
    for record in records:
        anchor = record["identity"][2] if record["identity"] else "<unanchored>"
        print(f"  #{record['number']:<5} {record['rule_id']}  {record['path']}:{record['start_line']}"
              f"  [{record['dismissed_reason']}]  {anchor[:60]}")
    return 0


def reserve(alert: dict, *buckets) -> None:
    """Claim an alert so that no second saved record can match it.

    Two dismissed findings can collapse onto one open alert - the same identity
    reached from two records, or the same generic message under ``--allow-fuzzy``.
    Without reserving the target, both records report success while only one
    dismissal survives; the other is lost silently, which is the one failure mode
    this script exists to prevent.
    """
    number = alert.get("number")
    for bucket in buckets:
        for key, alerts in list(bucket.items()):
            if any(a.get("number") == number for a in alerts):
                bucket[key] = [a for a in alerts if a.get("number") != number]


def line_distance(alert: dict, line) -> int:
    """How far an alert sits from a recorded line - the fuzzy tie-breaker."""
    location = (alert.get("most_recent_instance") or {}).get("location") or {}
    return abs((location.get("start_line") or 0) - (line or 0))


def dismiss(args, record: dict, target: dict, how: str) -> None:
    if args.dry_run:
        print(f"  WOULD     #{target['number']} <- #{record['number']} "
              f"{record['rule_id']} {record['path']} ({how})")
        return
    request(
        "PATCH",
        f"{API}/repos/{args.repo}/code-scanning/alerts/{target['number']}",
        {
            "state": "dismissed",
            "dismissed_reason": record["dismissed_reason"] or "won't fix",
            "dismissed_comment": (record["dismissed_comment"] or "")
            + f" [re-applied from alert #{record['number']} after a fingerprint migration]",
        },
    )
    print(f"  DISMISSED #{target['number']} <- #{record['number']} "
          f"{record['rule_id']} {record['path']} ({how})")


def cmd_apply(args) -> int:
    saved = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if not saved:
        print("Nothing to re-apply.")
        return 0

    current = list(paginate(args.repo, {"tool_name": args.tool, "state": "open"}))
    index = defaultdict(list)
    by_identity = defaultdict(list)
    chain_of: dict = {}
    # Open alerts that could not be anchored to source. They are invisible to
    # identity matching, so a record whose alert is one of them finds nothing -
    # which looks exactly like "the lint was fixed" and is its opposite: the
    # alert is open and about to stay un-dismissed. Tracking them by
    # (rule id, path) is what lets such a record be told apart from a benign MISS
    # instead of being counted as one and exiting 0.
    unanchored = []
    unanchored_by_key = defaultdict(list)
    for alert in current:
        index[key_of(alert)].append(alert)
        instance = alert.get("most_recent_instance") or {}
        location = instance.get("location") or {}
        identity = alert_identity(
            args.repository,
            (alert.get("rule") or {}).get("id"),
            location.get("path"),
            location,
        )
        if identity is not None:
            by_identity[identity].append(alert)
            chain_of[alert.get("number")] = identity[2]
        else:
            unanchored.append(alert)
            unanchored_by_key[
                ((alert.get("rule") or {}).get("id"), location.get("path"))
            ].append(alert)

    if unanchored:
        print(f"warning: {len(unanchored)} open alert(s) could not be anchored to "
              f"source in {args.repository}; no saved record can match them, and a "
              f"record pointing at one is reported UNRESOLVED rather than counted as "
              f"already fixed. Check that --repository is the commit the analysis "
              f"ran on:", file=sys.stderr)
        for alert in unanchored:
            print(f"  unanchored {describe(alert)}", file=sys.stderr)

    applied = missed = ambiguous = unresolved = conflicted = 0

    for record in saved:
        where = f"{record['rule_id']} {record['path']}:{record['start_line']}"
        url = record.get("html_url") or "<no url>"
        saved_identity = record.get("identity")
        stale = bool(saved_identity) and len(saved_identity) != IDENTITY_ARITY
        identity = tuple(saved_identity) if saved_identity and not stale else None

        # No comparable identity. The snapshot cannot be matched, and it cannot be
        # retaken either - the alerts it describes stopped existing at the
        # cutover. Guessing is what --allow-fuzzy would do, and on the shape a
        # migration actually produces (an insertion above a finding renumbers
        # every alert below it) the guess lands on a different, still-wanted alert
        # and reports success. So this is a hand review, and it says so plainly.
        if identity is None:
            if stale:
                reason = (f"its recorded identity has {len(saved_identity)} field(s), "
                          f"not this script's {IDENTITY_ARITY}: the snapshot predates "
                          f"the current identity shape")
            else:
                reason = "no identity was recorded for it at export time"
            print(f"  UNRESOLVED {where} - {reason}. It cannot be matched "
                  f"automatically, and the snapshot cannot be retaken after the "
                  f"cutover, so review it by hand from the old alert: {url}")
            print(f"             (--allow-fuzzy is deliberately not offered here: "
                  f"with no identity to corroborate, rule/path/message plus a line "
                  f"number picks whatever now sits on that line, which after an "
                  f"insertion is a different finding.)")
            unresolved += 1
            continue

        # Preferred: the reconstructed identity, which is far more selective than
        # Clippy's generic message text.
        exact = by_identity.get(identity, [])

        if len(exact) == 1:
            target = exact[0]
            dismiss(args, record, target, "identity match")
            reserve(target, by_identity, index)
            applied += 1
            continue

        if len(exact) > 1:
            print(f"  AMBIGUOUS {where} - {len(exact)} open alerts share this "
                  f"identity (expected where the old scheme collided and one alert "
                  f"stood for several findings); dismiss by hand, old alert {url}:")
            for alert in exact:
                print(f"             candidate {describe(alert)}")
            ambiguous += 1
            continue

        # An identity that matches nothing may mean the alert is gone, or that an
        # earlier record already claimed the only alert carrying it. Those are
        # very different outcomes: the second one is a dismissal about to be lost.
        if identity in by_identity:
            print(f"  CONFLICT  {where} - the only alert with this identity was "
                  f"already claimed by an earlier record; dismiss by hand ({url})")
            conflicted += 1
            continue

        # Before calling this benign, rule out the alert merely being invisible.
        blocked = unanchored_by_key.get((record["rule_id"], record["path"]), [])
        if blocked:
            print(f"  UNRESOLVED {where} - no open alert has this identity, but "
                  f"{len(blocked)} open alert(s) on this rule and path could not be "
                  f"anchored to source, so this is NOT evidence that the lint is "
                  f"fixed; dismiss by hand, old alert {url}:")
            for alert in blocked:
                print(f"             unanchored {describe(alert)}")
            unresolved += 1
            continue

        if not args.allow_fuzzy:
            # Not a failure: every open alert on this rule and path anchored, and
            # none carries this identity, so the lint was most likely fixed. Say
            # so, but mention the weaker candidates so the operator can judge
            # whether --allow-fuzzy is worth a second pass.
            weak = len(index.get((record["rule_id"], record["path"],
                                  record["message"]), []))
            hint = (f"; {weak} unclaimed alert(s) match rule/path/message, "
                    f"which only --allow-fuzzy would consider" if weak else "")
            print(f"  MISS      {where} - no open alert has this identity; "
                  f"it may already be fixed{hint}")
            missed += 1
            continue

        # --allow-fuzzy: (rule id, path, message), corroborated by the enclosing
        # item chain, with a nearest-line tie-break. The chain check is what keeps
        # the tie-break honest: after an insertion above the finding, the alert
        # nearest the recorded line is typically the new code that took that line
        # over, and it lives in a different item. Even so this is weaker than
        # identity and can still pick the wrong alert, which is why it is never
        # reached unless it was asked for.
        key = (record["rule_id"], record["path"], record["message"])
        candidates = index.get(key, [])
        if not candidates:
            if key in index:
                print(f"  CONFLICT  {where} - every alert matching rule/path/message "
                      f"was already claimed; dismiss by hand ({url})")
                conflicted += 1
            else:
                print(f"  MISS      {where} - no open alert matches; it may already "
                      f"be fixed")
                missed += 1
            continue

        corroborated = [
            a for a in candidates if chain_of.get(a.get("number")) == identity[2]
        ]
        if not corroborated:
            print(f"  UNRESOLVED {where} - {len(candidates)} alert(s) match "
                  f"rule/path/message but none of them is inside {identity[2]!r}, "
                  f"where this finding was; taking the nearest line would dismiss a "
                  f"different finding. Dismiss by hand, old alert {url}:")
            for alert in candidates:
                print(f"             candidate {describe(alert)} "
                      f"in {chain_of.get(alert.get('number'), '<unanchored>')!r}")
            unresolved += 1
            continue

        if len(corroborated) > 1:
            # Prefer the closest line; only accept it if that is unambiguous.
            ordered = sorted(corroborated,
                             key=lambda a: line_distance(a, record["start_line"]))
            if (line_distance(ordered[0], record["start_line"])
                    == line_distance(ordered[1], record["start_line"])):
                print(f"  AMBIGUOUS {where} - {len(corroborated)} equally close "
                      f"alerts in the same item; dismiss by hand ({url})")
                ambiguous += 1
                continue
            target = ordered[0]
        else:
            target = corroborated[0]

        dismiss(args, record, target, "fuzzy: rule/path/message in the same item")
        reserve(target, by_identity, index)
        applied += 1

    # Only a record that needed a decision nobody could make counts as a failure.
    # A record whose alert simply is not there any more is the expected outcome of
    # a migration that also fixed some lints, and must not fail the job - but it
    # only reaches that bucket once the unanchorable alerts above have been ruled
    # out as the reason it was not found.
    failures = ambiguous + unresolved + conflicted
    verb = "Would re-apply" if args.dry_run else "Re-applied"
    print(f"\n{verb} {applied}/{len(saved)}; {missed} already gone (expected); "
          f"{failures} need attention ({ambiguous} ambiguous, {unresolved} "
          f"unresolved, {conflicted} conflicting); "
          f"{len(unanchored)} open alert(s) could not be anchored to source.")
    if failures:
        print(f"error: {failures} dismissal(s) could not be re-applied safely; "
              f"dismiss them by hand", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--tool", default="clippy")
    parser.add_argument("--repository", type=Path, default=Path.cwd(),
                        help="checkout used to anchor alerts to source")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="snapshot dismissed alerts (run BEFORE the cutover)")
    export.add_argument("--out", type=Path, default=Path("clippy-dismissals.json"))
    export.set_defaults(func=cmd_export)

    apply_ = sub.add_parser("apply", help="re-dismiss matching alerts (run AFTER the cutover)")
    apply_.add_argument("--snapshot", type=Path, default=Path("clippy-dismissals.json"))
    apply_.add_argument("--dry-run", action="store_true")
    apply_.add_argument(
        "--allow-fuzzy",
        action="store_true",
        help="WARNING: CAN DISMISS THE WRONG ALERT. For a record that carries a "
        "comparable identity but matched no open alert, fall back to (rule id, "
        "path, message) - text Clippy repeats verbatim across unrelated findings "
        "- with a nearest-line tie-break, which after an insertion above the "
        "finding prefers whatever new code took that line over. Candidates in a "
        "different enclosing item are rejected, which blocks that common case, "
        "but two findings of the same lint in the same function are still told "
        "apart by line number alone. Records with no comparable identity are "
        "never matched this way; they are reported for hand review instead. "
        "Review every fuzzy match - run --dry-run first. Off by default.",
    )
    apply_.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
