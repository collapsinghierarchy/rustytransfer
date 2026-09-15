#!/usr/bin/env python3
"""Regression harness: assert Clippy finding identity survives code churn.

Clippy cannot be re-run for every hypothetical edit (it needs a toolchain and a
full build), so instead of regenerating diagnostics this harness *replays* them:
it applies a synthetic edit to the source, maps each finding's byte span through
the edit, and recomputes the fingerprint against the edited tree. A finding
whose identity is stable must produce a byte-identical fingerprint.

Each scenario is one of the change classes the fingerprint is supposed to
tolerate (reformatting, code motion, renames). Scenarios only ever assert that
a fingerprint is *unchanged*, so the cases the fingerprint must NOT tolerate -
two different findings colliding - live separately in CHECKS, which assert an
explicit relation between hand-built sources.

Usage::

    python3 .github/scripts/tests/test_identity.py target/inline-clippy.sarif .
    python3 .github/scripts/tests/test_identity.py

The first form replays a real Clippy report; ``target/inline-clippy.sarif`` is
the fixture that matches the committed sources (177 results, every byteOffset
agreeing with its own startLine/startColumn). The second form takes no SARIF at
all and synthesizes a corpus from the source tree, which is how CI runs this
guard before Clippy has produced anything. Those are the only two modes: a SARIF
path that was *given* but cannot be read is an error, never a quiet downgrade to
the synthesized corpus, because a typo would otherwise report a green stability
gate having never read a real finding.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import hashlib
import importlib.util
from collections import defaultdict

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from rustscan import IDENT, code_mask, masks, scan_items  # noqa: E402

# The stabilizer's filename contains a dash, so it cannot be imported by name.
_spec = importlib.util.spec_from_file_location(
    "stabilizer", SCRIPTS / "stabilize-clippy-sarif.py"
)
_stabilizer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stabilizer)
FINGERPRINT_VERSION = _stabilizer.FINGERPRINT_VERSION
decode = _stabilizer.decode
identity_parts = _stabilizer.identity_parts
identity_digest = _stabilizer.identity_digest


# --------------------------------------------------------------------------
# edit application with an offset map
# --------------------------------------------------------------------------

def apply_edits(src: str, edits):
    """edits: [(offset, delete_len, insert_text)]. Returns (new_src, map_fn)."""
    edits = sorted(edits, key=lambda e: e[0])
    out = []
    cursor = 0
    shifts = []  # (original_offset, cumulative_delta_after_here)
    delta = 0
    for offset, delete_len, insert_text in edits:
        out.append(src[cursor:offset])
        out.append(insert_text)
        delta += len(insert_text) - delete_len
        shifts.append((offset, delta))
        cursor = offset + delete_len
    out.append(src[cursor:])

    def remap(pos: int) -> int:
        result = pos
        for offset, cumulative in shifts:
            if pos >= offset:
                result = pos + cumulative
            else:
                break
        return result

    return "".join(out), remap


def reindent(src: str):
    """Add four spaces to the start of every non-empty line (formatting churn)."""
    edits = []
    pos = 0
    for line in src.splitlines(keepends=True):
        if line.strip():
            edits.append((pos, 0, "    "))
        pos += len(line)
    return apply_edits(src, edits)


def _call_shape(src: str, code: bytearray, open_paren: int):
    """(index of the matching ')', [top-level comma indexes]) for a call.

    Returns (None, []) when the call is not closed on its own line - this
    scenario only reflows calls that are currently written on one line.
    """
    depth = 0
    commas = []
    j = open_paren
    n = len(src)
    while j < n:
        if src[j] == "\n":
            return None, []
        if code[j]:
            ch = src[j]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    return (j, commas) if ch == ")" else (None, [])
            elif ch == "," and depth == 1:
                commas.append(j)
        j += 1
    return None, []


def reflow_calls(src: str):
    """Break every single-line call with 2+ arguments across lines, rustfmt style.

    This is the one formatting change that alters the *token stream* rather than
    just the whitespace between tokens: rustfmt appends a trailing comma to the
    last argument whenever it reflows a call vertically. Every other scenario in
    this file leaves the tokens alone, which is exactly how the trailing comma
    went unnoticed until it re-keyed alerts in the field.
    """
    code = code_mask(src)
    n = len(src)
    edits = []
    i = 0
    while i < n:
        # `foo(` - an identifier immediately before the paren. Macro calls end in
        # `!` and are skipped, as are tuples and grouping parens.
        if not (code[i] and src[i] == "(" and i and src[i - 1] in IDENT):
            i += 1
            continue
        close, commas = _call_shape(src, code, i)
        if close is None or not commas or commas[-1] == close - 1:
            i += 1
            continue
        line_start = src.rfind("\n", 0, i) + 1
        head = src[line_start:i]
        indent = head[: len(head) - len(head.lstrip(" \t"))]
        inner = indent + "    "
        edits.append((i + 1, 0, "\n" + inner))
        for comma in commas:
            edits.append((comma + 1, 0, "\n" + inner))
        edits.append((close, 0, ",\n" + indent))
        i += 1
    return apply_edits(src, edits)


# --------------------------------------------------------------------------
# fingerprint recomputation (v5, via the shipped stabilizer)
# --------------------------------------------------------------------------

def fingerprints(findings):
    """findings: [(path, rule, src, offset, length)] -> {index: fingerprint}

    Every identity input comes from the shipped stabilizer, never from a copy
    here. An earlier version of this harness recomputed the preimage inline and
    silently fell a tier behind the real algorithm, reporting stability the
    shipped code did not have.
    """
    per_file = {}
    records = []
    for index, (path, rule, src, offset, length) in enumerate(findings):
        if path not in per_file:
            code, comment = masks(src)
            per_file[path] = (scan_items(src), comment, code)
        items, comments, code = per_file[path]
        parts = identity_parts(src, items, comments, code, offset, length)
        digest = identity_digest(rule, path, parts)
        records.append((index, path, parts["chain"], digest, offset))

    groups = defaultdict(list)
    for record in records:
        groups[(record[1], record[2], record[3])].append(record)
    result = {}
    for key, bucket in groups.items():
        bucket.sort(key=lambda r: r[4])
        for ordinal, (index, _, _, digest, _) in enumerate(bucket, start=1):
            result[index] = f"{digest}:{ordinal}"
    return result


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

def _line_table(src: str):
    """[(start offset, line text)] for `src`, offsets in bytes."""
    table = []
    offset = 0
    for line in src.splitlines(keepends=True):
        table.append((offset, line))
        offset += len(line)
    return table


def _offset_disagrees(table, region: dict) -> bool:
    """Does `byteOffset` contradict the `startLine`/`startColumn` beside it?

    clippy-sarif derives both from the same span, so in a report generated
    against the sources as they stand they always agree. When they do not, the
    report was produced against a *different* revision of the file and every
    number this harness prints is measured against spans pointing at the wrong
    bytes - a silently meaningless run, because a wrong-but-in-range offset
    still fingerprints cleanly and still matches itself under every scenario.

    This is exactly how two fixtures in ``target/`` were caught after both had
    been used: ``current-clippy.sarif`` disagrees on 2 of its 177 results and
    ``clippy.sarif`` on 24 of its 390, while ``inline-clippy.sarif`` - the one to
    use - disagrees on none. The detector is baked in so that the next stale
    fixture announces itself instead of being found by hand.

    Columns are counted in characters and ``byteOffset`` in bytes, so a start
    line holding any non-ASCII byte is skipped rather than reported: there the
    two legitimately differ. (Sources are decoded latin-1, one character per
    byte, so line *lengths* are already exact byte counts.)
    """
    if "byteOffset" not in region or "startLine" not in region:
        return False
    start_line = int(region["startLine"])
    if not 0 < start_line <= len(table):
        # The file is shorter than the report claims: stale beyond argument.
        return True
    offset, line = table[start_line - 1]
    if any(ch > "\x7f" for ch in line):
        return False
    return offset + int(region.get("startColumn", 1)) - 1 != int(region["byteOffset"])


def load_findings(sarif_path: Path, repo: Path):
    """Returns (findings, sources, meta). `meta` is for the startup banner."""
    sarif = json.loads(sarif_path.read_text(encoding="utf-8"))
    sources = {}
    lines = {}
    out = []
    total = 0
    seen = defaultdict(int)
    repeats = 0
    stale = []
    for run in sarif.get("runs", []):
        for result in run.get("results", []):
            total += 1
            loc = result["locations"][0]["physicalLocation"]
            path = loc["artifactLocation"]["uri"]
            region = loc["region"]
            if "byteOffset" not in region:
                continue
            if path not in sources:
                sources[path] = decode((repo / path).read_bytes())
                lines[path] = _line_table(sources[path])
            if _offset_disagrees(lines[path], region):
                stale.append((path, result.get("ruleId", ""),
                              int(region["byteOffset"]), int(region["startLine"])))
            record = (path, result.get("ruleId", ""),
                      int(region["byteOffset"]), int(region["byteLength"]))
            seen[record] += 1
            if seen[record] > 1:
                repeats += 1
            out.append(
                (record[0], record[1], sources[path], record[2], record[3])
            )
    meta = {"unit": "SARIF result", "results": total, "spanned": len(out),
            "files": len(sources), "repeats": repeats, "stale": stale}
    return out, sources, meta


def banner(origin: str, meta: dict):
    """One line naming the corpus under test, loud enough to read in a CI log.

    A report produced before the Clippy invocation was narrowed to ``--lib
    --bins`` contains every finding twice - once per compilation target - and
    still fingerprints cleanly, because the ordinal separates the copies. A
    report generated against an older revision of the sources fingerprints
    cleanly too, and passes every scenario, because a span that points at the
    wrong bytes still points at the *same* wrong bytes before and after an edit.

    Neither is visible from the result the run prints, so both go on the first
    lines it prints instead: the count, the repeat warning, and the count of
    results whose byteOffset contradicts their own startLine/startColumn.
    """
    print(f"corpus: {origin}")
    print(f"  {meta['results']} {meta['unit']}(s), {meta['spanned']} with byte spans, "
          f"{meta['files']} source file(s)")
    if meta["repeats"]:
        print(f"  WARNING: {meta['repeats']} result(s) repeat an earlier "
              f"rule+file+span exactly - this report looks DOUBLED (a stale run "
              f"from before `--lib --bins`), not a clean corpus")
    stale = meta.get("stale") or []
    if stale:
        print(f"  WARNING: {len(stale)} of {meta['spanned']} result(s) have a "
              f"byteOffset that disagrees with their own startLine/startColumn - "
              f"this report was NOT generated against the current sources, so "
              f"every span below points at the wrong bytes")
        for path, rule, offset, line in stale[:3]:
            print(f"        {rule} {path}@{offset} (reported at line {line})")
        if len(stale) > 3:
            print(f"        ... and {len(stale) - 3} more")


# Substrings that recur often enough in this codebase to exercise the duplicate
# path, paired with a plausible lint so the synthetic corpus resembles real
# Clippy output.
SYNTHETIC_PATTERNS = [
    ("|_|", "clippy::map_err_ignore"),
    (".unwrap()", "clippy::unwrap_used"),
    (".expect(", "clippy::expect_used"),
    (".await", "clippy::redundant_clone"),
    (" as u64", "clippy::as_conversions"),
    (" as u32", "clippy::as_conversions"),
    ("continue", "clippy::needless_continue"),
]


def synthesize_findings(repo: Path):
    """Build a findings corpus from the source tree, with no Clippy run needed.

    CI runs this guard before Clippy has produced anything, and a committed SARIF
    fixture would go stale against the code it indexes. Sampling the tree
    directly keeps the test self-contained and keeps it exercising whatever the
    source actually looks like today, duplicates included.
    """
    sources = {}
    out = []
    files = sorted(
        list(repo.glob("src/**/*.rs")) + list(repo.glob("tests/**/*.rs"))
    )
    for file in files:
        path = file.relative_to(repo).as_posix()
        text = decode(file.read_bytes())
        sources[path] = text
        for item in scan_items(text):
            end = text.find("\n", item["start"])
            end = item["body_start"] if end < 0 else min(end, item["body_start"])
            out.append((path, "clippy::missing_docs_in_private_items", text,
                        item["start"], max(1, end - item["start"])))
        for needle, rule in SYNTHETIC_PATTERNS:
            start = text.find(needle)
            while start >= 0:
                out.append((path, rule, text, start, len(needle)))
                start = text.find(needle, start + 1)
    return out, sources


def rebuild(findings, sources, edited_sources, remaps):
    """Re-express each finding against the edited source.

    Both ends of the span are remapped, not just the start: a reformat widens a
    multi-line span, and real Clippy would report the widened byte length. Holding
    the length fixed would measure an artifact of the harness rather than of the
    fingerprint.
    """
    rebuilt = []
    for path, rule, _src, offset, length in findings:
        new_src = edited_sources.get(path, sources[path])
        remap = remaps.get(path)
        if remap is None:
            rebuilt.append((path, rule, new_src, offset, length))
            continue
        new_offset = remap(offset)
        new_end = remap(offset + length)
        rebuilt.append((path, rule, new_src, new_offset, max(0, new_end - new_offset)))
    return rebuilt


SCENARIOS = {}


def scenario(name):
    def register(fn):
        SCENARIOS[name] = fn
        return fn
    return register


@scenario("insert 10 blank lines at the top of every file")
def _s1(sources):
    edited, remaps = {}, {}
    for path, src in sources.items():
        edited[path], remaps[path] = apply_edits(src, [(0, 0, "\n" * 10)])
    return edited, remaps


@scenario("insert a license header comment in every file")
def _s2(sources):
    header = "// Copyright 2026 rustytransfer\n// SPDX-License-Identifier: MIT\n\n"
    edited, remaps = {}, {}
    for path, src in sources.items():
        edited[path], remaps[path] = apply_edits(src, [(0, 0, header)])
    return edited, remaps


@scenario("reindent every line by 4 spaces (formatting churn)")
def _s3(sources):
    edited, remaps = {}, {}
    for path, src in sources.items():
        edited[path], remaps[path] = reindent(src)
    return edited, remaps


@scenario("convert every line ending to CRLF")
def _s4(sources):
    edited, remaps = {}, {}
    for path, src in sources.items():
        edits = []
        pos = 0
        for line in src.splitlines(keepends=True):
            if line.endswith("\n"):
                edits.append((pos + len(line) - 1, 0, "\r"))
            pos += len(line)
        edited[path], remaps[path] = apply_edits(src, edits)
    return edited, remaps


@scenario("append a trailing comment to the first line of each function body")
def _s5(sources):
    edited, remaps = {}, {}
    for path, src in sources.items():
        edits = []
        for item in scan_items(src):
            if item["kind"] != "fn":
                continue
            newline = src.find("\n", item["body_start"])
            if 0 < newline < item["body_end"]:
                edits.append((newline, 0, "  // NOTE: audited"))
        edited[path], remaps[path] = apply_edits(src, edits)
    return edited, remaps


@scenario("reflow multi-argument calls across lines with a trailing comma (rustfmt)")
def _s6(sources):
    edited, remaps = {}, {}
    for path, src in sources.items():
        edited[path], remaps[path] = reflow_calls(src)
    return edited, remaps


# --------------------------------------------------------------------------
# explicit relations between two hand-built sources
# --------------------------------------------------------------------------
#
# A scenario can only say "this edit changed nothing". The interesting negative
# property - two findings that differ only inside a string literal must NOT
# share an identity - is a relation between two *different* sources, so it needs
# its own code path rather than a scenario that pretends to be an edit.

CHECKS = {}


def check(name):
    def register(fn):
        CHECKS[name] = fn
        return fn
    return register


_CHECK_PATH = "src/synthetic.rs"
_CHECK_RULE = "clippy::needless_borrow"


def _at(src: str, start: int, length: int) -> str:
    """Fingerprint of one finding at an explicit span, as the file's only one."""
    return fingerprints([(_CHECK_PATH, _CHECK_RULE, src, start, length)])[0]


def _parts_at(src: str, start: int, length: int) -> dict:
    """The three identity tiers for that finding, so a check can name the tier.

    A check that only ever compares whole fingerprints cannot tell which tier
    did the separating, which is how ``_c1`` came to pass for the wrong reason.
    """
    code, comment = masks(src)
    return identity_parts(src, scan_items(src), comment, code, start, length)


def _single(src: str):
    """Span of the one `warn(..)` call in `src`."""
    start = src.index("warn(")
    return start, src.index(")", start) + 1 - start


# --------------------------------------------------------------------------
# _c1: the SPAN tier in isolation
#
# Two whole files, byte-identical but for the run of spaces inside one string
# literal, and the finding is the whole `const` item. A bodiless item is its own
# header, so `context` is the literal "<hdr>" for both and `chain` is
# "const BANNER" for both: `span` is the only tier that can tell them apart, and
# the check therefore fails if and only if the span tier stops preserving
# whitespace inside a literal.
#
# The earlier form of this check used a `warn("...")` statement, where the span
# and the enclosing statement are the same text. Both tiers then differed, so
# mutating `normalize_span` alone - collapsing whitespace instead of preserving
# it inside literals - still left the context tier keeping the two fingerprints
# apart, and the check reported PASS. It asserted its own name and could not
# fail on the regression it was named for.
# --------------------------------------------------------------------------

_SPAN_WIDE = 'const BANNER: &str = "bad  header";\n'
_SPAN_NARROW = 'const BANNER: &str = "bad header";\n'


def _whole_item(src: str):
    return 0, src.index(";") + 1


@check("whitespace inside a string literal must NOT collide (span tier, isolated)")
def _c1():
    wide = _parts_at(_SPAN_WIDE, *_whole_item(_SPAN_WIDE))
    narrow = _parts_at(_SPAN_NARROW, *_whole_item(_SPAN_NARROW))
    if wide["chain"] != narrow["chain"] or wide["context"] != narrow["context"]:
        return ("this check no longer ISOLATES the span tier - chain "
                f"{wide['chain']!r} vs {narrow['chain']!r}, context "
                f"{wide['context']!r} vs {narrow['context']!r}. Rebuild the pair "
                "so the two sources differ only inside the span, or it will pass "
                "on another tier's separation and hide a span-tier regression")
    if wide["span"] == narrow["span"]:
        return (f"both spellings produced span {wide['span']!r}; a literal's "
                f"content is part of what the finding is about, so collapsing it "
                f"merges two alerts")
    hot, cold = (_at(_SPAN_WIDE, *_whole_item(_SPAN_WIDE)),
                 _at(_SPAN_NARROW, *_whole_item(_SPAN_NARROW)))
    if hot == cold:
        return f"the spans differ but both findings hashed to {hot}"
    return None


# Identical but for the run of spaces inside the literal.
_LIT_WIDE = 'fn emit() {\n    warn("bad  header");\n}\n'
_LIT_NARROW = 'fn emit() {\n    warn("bad header");\n}\n'
# The same call as _LIT_WIDE after rustfmt broke it across lines.
_LIT_REFLOWED = 'fn emit() {\n    warn(\n        "bad  header",\n    );\n}\n'


@check("whitespace inside a string literal must NOT collide (whole finding)")
def _c1b():
    wide = _at(_LIT_WIDE, *_single(_LIT_WIDE))
    narrow = _at(_LIT_NARROW, *_single(_LIT_NARROW))
    if wide == narrow:
        return (f"both spellings hashed to {wide}; two different diagnostics "
                f"merged into one alert")
    return None


@check("a rustfmt reflow of that same call must NOT move the fingerprint")
def _c2():
    wide = _at(_LIT_WIDE, *_single(_LIT_WIDE))
    reflowed = _at(_LIT_REFLOWED, *_single(_LIT_REFLOWED))
    if wide != reflowed:
        return f"one line -> {wide}, reflowed -> {reflowed}"
    return None


# --------------------------------------------------------------------------
# doc comments: the identity of a finding INSIDE an item's documentation
#
# 36 findings in this repository (missing_errors_doc, missing_panics_doc,
# doc_markdown) sit inside a `///` block rather than in code. While the comment
# sat outside the item it documents, those findings sat outside every item:
# `context` fell back to a 300-byte window of the FOLLOWING function's body, so
# renaming a local in that body re-keyed the doc alert and dropped its
# dismissal. Anchoring the doc comment to the item fixes that by returning
# "<hdr>" - but "<hdr>" is a constant, so this pair of checks has to hold both
# ends at once: invariant to the body, and still distinct from its own sibling.
# --------------------------------------------------------------------------

_DOC = (
    "/// Reads a `DataChannel` off the wire.\n"
    "///\n"
    "/// # Errors\n"
    "/// Returns an error when the `WebRtcState` has closed.\n"
    "pub fn read_channel(app_id: &str) -> Result<DataChannel> {\n"
    "    let ws = Ws::new(app_id);\n"
    "    Ok(DataChannel::new(ws))\n"
    "}\n"
)
# A rename of a local inside the function BODY. Nothing above `pub fn` moves, so
# a doc finding's byte span is untouched and any change of fingerprint is the
# fingerprint's own doing.
_DOC_EDITED = _DOC.replace("ws", "socket_handle")

# Two doc findings in the SAME comment, as clippy::doc_markdown reports them:
# one per backticked item that should be in code brackets.
_DOC_SPANS = ("`DataChannel`", "`WebRtcState`")


def _doc_fingerprints(src: str):
    spans = [(src.index(needle), len(needle)) for needle in _DOC_SPANS]
    return fingerprints(
        [(_CHECK_PATH, "clippy::doc_markdown", src, start, length)
         for start, length in spans]
    )


@check("a doc finding must NOT move when the documented body is edited")
def _c3():
    before, after = _doc_fingerprints(_DOC), _doc_fingerprints(_DOC_EDITED)
    moved = [i for i in before if before[i] != after.get(i)]
    if moved:
        return (f"{len(moved)} of {len(before)} doc finding(s) re-keyed when a "
                f"local was renamed inside the function they document: "
                f"{before[moved[0]]} -> {after.get(moved[0])}")
    return None


@check("two doc findings in the SAME comment must NOT collide")
def _c4():
    # Compare the DIGEST, not the fingerprint: the ordinal would paper over a
    # collision with `:1`/`:2` and so hide exactly what this check is for. An
    # ordinal is a backstop, not an identity - it renumbers whenever a doc line
    # is added above, closing every alert below it.
    digests = [value.split(":")[0] for value in _doc_fingerprints(_DOC).values()]
    if len(set(digests)) != len(digests):
        return (f"{len(digests)} doc findings in one comment share "
                f"{len(set(digests))} digest(s); they are anchored to the same "
                f"<hdr> context and the same chain, so the span is all that can "
                f"separate them")
    return None


def measure_duplicate_removal(findings, baseline):
    """Report the blast radius of deleting one of a set of identical findings.

    This is the one case an ordinal cannot make free. Findings before the deleted
    one keep their number; findings after it shift down by one and so change
    identity. The cost is reported rather than asserted, because it is inherent:
    without a persistent registry there is no way to tell "the second |_| was
    deleted" from "the last |_| was deleted".

    The *first* member of each group is the one deleted, because that is the
    worst case: every survivor renumbers, so the number printed below is the
    ceiling on the churn. Deleting the middle member measures nothing for the
    commonest group size - a pair, where the middle element *is* the last one,
    leaving the single survivor on ordinal ``:1`` and reporting zero churn for a
    deletion that in general does re-key alerts.
    """
    groups = defaultdict(list)
    for index, value in baseline.items():
        groups[value.split(":")[0]].append(index)
    duplicated = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
    if not duplicated:
        print("  (no duplicate findings in this corpus)")
        return

    total_churn = 0
    for digest, indexes in sorted(duplicated.items()):
        survivors = indexes[1:]
        after = fingerprints([findings[i] for i in survivors])
        moved = sum(
            1
            for position, original in enumerate(survivors)
            if after[position] != baseline[original]
        )
        total_churn += moved
        path, rule, _s, off, _l = findings[indexes[0]]
        print(f"    x{len(indexes)} {rule} {path}: deleting the FIRST of them "
              f"re-opens {moved} of the {len(survivors)} survivors")
    print(f"  worst case: {total_churn} alert(s) would churn across "
          f"{len(duplicated)} duplicate group(s)")


def run(sarif_path, repo: Path):
    if sarif_path is not None:
        # A path that was GIVEN but cannot be read is an error, not a fallback.
        # Downgrading to the synthesized corpus here printed "no SARIF report
        # given" - which was false, one had been given - and then exited 0, so a
        # typo'd path reported a green stability gate having never read a single
        # real finding. The synthesized corpus is a different, weaker test; it is
        # for the run that asked for it by passing no argument at all.
        if not sarif_path.is_file():
            print(f"corpus: {sarif_path}  <- UNREADABLE", flush=True)
            print(f"ERROR: no such SARIF report: {sarif_path}", file=sys.stderr)
            print("       A path was given, so the synthesized fallback is NOT "
                  "used - it would report a green stability gate over findings "
                  "this run never read.", file=sys.stderr)
            print("       Pass target/inline-clippy.sarif, or no argument at all "
                  "to synthesize a corpus from the source tree.", file=sys.stderr)
            return 2
        findings, sources, meta = load_findings(sarif_path, repo)
        origin = str(sarif_path)
    else:
        findings, sources = synthesize_findings(repo)
        origin = ("<synthesized from the source tree: no SARIF path was given "
                  "on the command line>")
        meta = {"unit": "synthetic finding", "results": len(findings),
                "spanned": len(findings), "files": len(sources), "repeats": 0,
                "stale": []}
    banner(origin, meta)
    baseline = fingerprints(findings)
    distinct = len(set(baseline.values()))
    print(f"baseline: {len(findings)} findings, {distinct} distinct identities")
    if distinct != len(findings):
        collided = defaultdict(list)
        for index, value in baseline.items():
            collided[value].append(index)
        print("  FAIL: baseline is not collision-free")
        for value, indexes in list(collided.items()):
            if len(indexes) > 1:
                path, rule, _s, off, _l = findings[indexes[0]]
                print(f"        x{len(indexes)} {rule} {path}@{off}")
        return 1

    failures = 0
    for name, build in SCENARIOS.items():
        edited, remaps = build(sources)
        after = fingerprints(rebuild(findings, sources, edited, remaps))
        moved = [i for i in baseline if baseline[i] != after.get(i)]
        status = "PASS" if not moved else f"FAIL ({len(moved)}/{len(findings)} moved)"
        if moved:
            failures += 1
        print(f"  [{status:22}] {name}")
        for index in moved[:3]:
            path, rule, _s, off, _l = findings[index]
            print(f"        {rule} {path}@{off}")

    for name, probe in CHECKS.items():
        problem = probe()
        if problem:
            failures += 1
        print(f"  [{'PASS' if not problem else 'FAIL':22}] {name}")
        if problem:
            print(f"        {problem}")
    print()
    print("duplicate-removal blast radius (informational):")
    measure_duplicate_removal(findings, baseline)
    print()
    print("PASS" if not failures else f"{failures} scenario(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sarif = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    repo = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
    raise SystemExit(run(sarif, repo.resolve()))
