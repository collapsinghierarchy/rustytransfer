#!/usr/bin/env python3
"""Add stable, repository-specific fingerprints to a Clippy SARIF report.

Identity model (v5)
-------------------
GitHub code scanning reads exactly one correlation key per result,
``partialFingerprints.primaryLocationLineHash``; every other key is ignored, and
a changed value closes the old alert and opens a new, unlinked one with no
dismissal history. So the whole identity has to fit in one string.

Identity is built in three tiers, each answering a different question:

  1. ``chain``   - WHERE: the nesting of named items containing the finding,
                   outermost first (``impl SenderFsm > fn step``, which is the
                   chain of 11 findings in this repository). A named item
                   contributes its kind and its bare identifier, never its
                   signature, so editing a parameter or a return type does not
                   re-key every finding in the body. Containment starts at the
                   item's declaration keyword - in fact at the first byte of the
                   doc comment above it - not at its opening brace, so a lint
                   that fires on a signature or inside its documentation anchors
                   to the item it describes instead of falling through to
                   ``<file-scope>``.

                   An ``impl`` block has no identifier, so its header stands in
                   for one, and there the generic *arguments* are kept
                   deliberately: ``src/protocol/fsm.rs`` holds
                   ``impl From<&str> for StepError`` (line 52),
                   ``impl From<String> for StepError`` (line 58) and
                   ``impl From<aes_gcm::Error> for StepError`` (line 64) side by
                   side, each with its own ``fn from``, and the arguments are the
                   only thing telling those three apart.

                   The generic *parameter* list immediately after ``impl`` - the
                   ``<T: Clone>`` of ``impl<T: Clone> Wrap<T>`` - is dropped.
                   What that buys is that the **bounds** stay out of identity:
                   widening ``impl<T: Clone>`` to ``impl<T: Clone + Send>``
                   re-keys nothing in the block. It does **not** make a parameter
                   *rename* free. The parameter almost always reappears in
                   argument position, where it *is* kept, so
                   ``impl<T: Clone> Wrap<T>`` and ``impl<U: Clone> Wrap<U>``
                   label as ``impl Wrap<T>`` and ``impl Wrap<U>`` and give
                   chains ``impl Wrap<T> > fn g`` and ``impl Wrap<U> > fn g`` -
                   the rename re-keys every finding in the block. Only a
                   parameter appearing nowhere in the impl's type is free to
                   rename.

                   ``impl`` in *type* position (``-> impl Iterator<Item = u32>``,
                   ``x: impl Into<String>``) is not an impl block and never
                   enters the chain. No such signature exists under ``src/``
                   today, so this costs the repository nothing now; it stops a
                   future one from swallowing its enclosing item.
  2. ``span``    - WHAT: the diagnostic's own text, token-normalized. The span is
                   lexed and its tokens rejoined with no separator, except where
                   two word/literal tokens would fuse into one, so line breaks
                   and indentation are invisible; the trailing commas rustfmt
                   inserts when it reflows a call or a literal across several
                   lines are dropped for the same reason. The one trailing comma
                   kept is the comma of a one-element tuple, because ``(a,)`` and
                   ``(a)`` are genuinely different expressions.

                   String, byte-string, raw-string and char literals are the
                   exception to normalization - they are emitted verbatim, so
                   their content, *including internal whitespace*, stays part of
                   the identity and ``"a  b"`` never collides with ``"a b"``.

                   Comment bytes are normally removed outright, so rewording a
                   trailing ``// why`` inside a multi-line span cannot move the
                   fingerprint. A span that is *entirely* comment is the
                   exception, because there the comment is the finding's subject:
                   it is tokenized like any other text. That is the whole
                   identity of the four ``clippy::doc_markdown`` results, whose
                   spans are ``DataChannel`` and ``WebRtcState`` in each of
                   ``src/transport/offerer.rs`` and ``src/transport/answerer.rs``.
  3. ``context`` - WHICH ONE: the smallest enclosing statement or match arm,
                   token-normalized by the same rules. This is what separates
                   findings that are textually identical, and it is why no
                   positional counter is needed: the four ``|_|`` closures in
                   ``fn parse_smt_header`` all have the span ``|_|``, but their
                   statements carry "Bad KEM ciphertext length", "Bad
                   nonce_prefix length", "Bad file_len" and "Bad chunk_size".

                   A finding on an item's **header** - anywhere from the first
                   byte of its doc comment to its opening brace - returns the
                   literal ``<hdr>`` instead. ``chain`` already identifies those
                   exactly, and a statement window around a signature would run
                   into the function body and import all of its churn. 85 of this
                   repository's 177 findings take that path, among them all 36
                   documentation lints (22 ``clippy::missing_errors_doc``, 10
                   ``clippy::missing_panics_doc``, 4 ``clippy::doc_markdown``),
                   which are the ones most likely to be bulk-dismissed and so the
                   ones that must not move when an unrelated statement in the
                   body changes.

Deliberately excluded:

  * line and column numbers   - a line number moves on every insertion above the
                                finding; a column moves on every re-indent
  * the git blame commit      - rewritten by cargo fmt, squash-merge, rebase and
                                amend, none of which change the code's meaning
  * the full item signature   - changes when a parameter or return type is edited
  * the SARIF message text    - display-only to GitHub, and it embeds inferred
                                types that drift with each toolchain release

An occurrence ordinal is still appended as ``:N``, mirroring the convention in
github/codeql-action's own fingerprints (``39fa2ee980eb94b0:1``). It is a
backstop for the case where the three tiers genuinely cannot tell two findings
apart, and it lives outside the digest so that changing the tie-break policy
re-keys only ambiguous findings. On this repository every emitted ordinal is
currently ``:1``: all 177 findings separate on content alone, which means
deleting one of a set of duplicates leaves the survivors byte-identical instead
of renumbering them.

Changing this algorithm
-----------------------
``FINGERPRINT_VERSION`` and the three tiers above are the entire identity.
Change any of them and every alert re-keys at once: GitHub closes each old alert
as *fixed* and opens a new, unlinked one carrying no dismissal history.

There is no migration tooling and none is planned. The procedure is deliberate
and destructive - delete the repository's existing code-scanning analyses and
let the next run repopulate them from clean:

    gh api -X DELETE "repos/<owner>/<repo>/code-scanning/analyses/<id>"

following ``next_analysis_url`` through the set and passing
``?confirm_delete=true`` on the last one. Re-apply any dismissals by hand
afterwards; deleting analyses is irreversible.

If that ever becomes too expensive to repeat, the mapping needed to carry
dismissals across is available without reconstructing anything from line and
column. The plain alerts API does not expose fingerprints, but fetching a
previous analysis as SARIF does:

    GET /repos/{owner}/{repo}/code-scanning/analyses/{id}
    Accept: application/sarif+json

That returns the document as uploaded - ``partialFingerprints`` intact - and
GitHub adds ``properties["github/alertNumber"]`` to every result. Pair it with a
local re-run of the old and new algorithms over one ``clippy.json`` and the
old-to-new map is exact, with no line-proximity guessing anywhere in it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rustscan import (  # noqa: E402
    enclosing_context,
    item_chain,
    masks,
    normalize_tokens,
    scan_items,
)

FINGERPRINT_VERSION = "rustytransfer-clippy-v6-probe"


def decode(raw: bytes) -> str:
    """Decode source so that string index == byte offset, exactly.

    Clippy reports spans as byte offsets into the original file. Decoding as
    UTF-8 breaks that correspondence for any non-ASCII byte, and reading in text
    mode additionally collapses CRLF to LF - on a Windows checkout of this
    repository that shifts every offset in the file by one per preceding line.
    latin-1 is a total, order-preserving byte-to-character map, so offsets stay
    exact. The decoded text is only ever scanned for ASCII syntax and hashed, so
    the mojibake a non-UTF-8 interpretation produces is immaterial - it is stable,
    which is all identity requires.
    """
    return raw.decode("latin-1")


def normalized_path(uri: str, repository: Path):
    uri = unquote(uri)
    if uri.startswith("file://"):
        uri = urlparse(uri).path
    source = Path(uri)
    if not source.is_absolute():
        source = repository / source
    try:
        source = source.resolve()
        relative = source.relative_to(repository.resolve())
    except (ValueError, OSError):
        return None
    return relative.as_posix(), source


def normalize_span(text: str, comments: bytearray, offset: int) -> str:
    """Token-normalize the diagnostic span, dropping comments.

    ``text`` is the span itself, ``comments`` the whole file's comment mask from
    :func:`rustscan.masks`, and ``offset`` the span's byte offset within that
    file - the mask is indexed absolutely, so it is windowed onto the span first.

    The work is done by :func:`rustscan.normalize_tokens`, the same normalizer
    the ``context`` tier uses, so the two tiers can never disagree about what a
    formatting-only change looks like. The span is lexed and its tokens rejoined
    with no separator except where two word/literal tokens would fuse into one,
    and rustfmt's trailing reflow commas are dropped, so re-indenting or
    re-wrapping the code cannot move the fingerprint. String, byte-string and
    raw-string literals are emitted verbatim: their content, *including internal
    whitespace*, is preserved and stays part of the identity, because it is part
    of what the finding is about.

    Comment bytes are normally removed outright, so editing a trailing ``// why``
    inside a multi-line span cannot move the fingerprint either.

    A span that is *entirely* comment is the exception. Doc lints such as
    ``clippy::doc_markdown`` and ``clippy::missing_errors_doc`` point at a doc
    comment, and there the comment is the subject rather than incidental noise.
    Stripping it would reduce every doc finding in a file to the same empty span,
    leaving them distinguishable only by ordinal and so renumbered whenever a doc
    comment is added above. So when the comment-skipping pass comes back empty,
    run it again with no mask, tokenizing the comment text like any other text.
    """
    window = None
    if comments is not None:
        window = comments[offset : offset + len(text)]
    stripped = normalize_tokens(text, 0, len(text), window)
    if stripped:
        return stripped
    return normalize_tokens(text, 0, len(text), None)


def region_span(result: dict, source: str):
    """(byte offset, byte length) of a result's primary region."""
    region = result["locations"][0]["physicalLocation"]["region"]
    offset = region.get("byteOffset")
    length = region.get("byteLength")
    if offset is not None and length is not None:
        return int(offset), int(length)
    # clippy-sarif 0.8.0 always emits byte offsets; this is belt and braces.
    start_line = int(region.get("startLine", 1))
    lines = source.splitlines(keepends=True)
    offset = sum(len(line) for line in lines[: start_line - 1])
    line = lines[start_line - 1] if 0 < start_line <= len(lines) else ""
    return offset, len(line)


def identity_parts(source: str, items, comments, code, offset: int, length: int):
    """The three identity tiers for one finding, plus its position.

    The regression harness calls this directly rather than reimplementing it, so
    that the algorithm under test cannot drift away from the algorithm shipped.
    """
    return {
        "offset": offset,
        "chain": item_chain(items, offset),
        "span": normalize_span(source[offset : offset + length], comments, offset),
        "context": enclosing_context(source, code, items, offset, comments),
    }


def identity_preimage(rule_id: str, relative_path: str, parts: dict) -> str:
    return "\0".join(
        (
            FINGERPRINT_VERSION,
            rule_id,
            relative_path,
            parts["chain"],
            parts["span"],
            parts["context"],
        )
    )


def identity_digest(rule_id: str, relative_path: str, parts: dict) -> str:
    return hashlib.sha256(
        identity_preimage(rule_id, relative_path, parts).encode("utf-8")
    ).hexdigest()


def stabilize(sarif: dict, repository: Path, verbose: bool = False):
    cache: dict[str, tuple] = {}
    pending = []
    skipped = 0

    for run in sarif.get("runs", []):
        for result in run.get("results", []):
            locations = result.get("locations") or []
            if not locations:
                skipped += 1
                continue
            artifact = locations[0].get("physicalLocation", {}).get("artifactLocation", {})
            uri = artifact.get("uri")
            if not isinstance(uri, str):
                skipped += 1
                continue
            resolved = normalized_path(uri, repository)
            if resolved is None:
                skipped += 1
                continue
            relative_path, source_path = resolved

            if relative_path not in cache:
                try:
                    raw = decode(source_path.read_bytes())
                except OSError:
                    cache[relative_path] = None
                else:
                    code, comment = masks(raw)
                    cache[relative_path] = (raw, scan_items(raw), comment, code)
            entry = cache[relative_path]
            if entry is None:
                skipped += 1
                continue
            source, items, comments, code = entry

            try:
                offset, length = region_span(result, source)
                parts = identity_parts(source, items, comments, code, offset, length)
            except (KeyError, ValueError, IndexError):
                skipped += 1
                continue

            digest = identity_digest(result.get("ruleId", ""), relative_path, parts)
            pending.append((result, relative_path, parts, digest))

    # Deterministic per-(file, item-chain, digest) ordinal, ordered by position.
    groups: dict[tuple, list] = defaultdict(list)
    for record in pending:
        _, relative_path, parts, digest = record
        groups[(relative_path, parts["chain"], digest)].append(record)

    for key, records in groups.items():
        records.sort(key=lambda r: r[2]["offset"])
        for ordinal, (result, relative_path, parts, digest) in enumerate(records, start=1):
            value = f"{digest}:{ordinal}"
            result["partialFingerprints"] = {"primaryLocationLineHash": value}
            if verbose:
                print(
                    f"  {value[:16]}..:{ordinal}  {relative_path}  "
                    f"{parts['chain'][:48]}  {parts['span'][:40]!r}",
                    file=sys.stderr,
                )

    return len(pending), skipped, groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sarif", type=Path)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--fail-on-skipped",
        action="store_true",
        help="exit non-zero if any result could not be fingerprinted (they would "
        "silently fall back to GitHub's own algorithm)",
    )
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    sarif_path = args.sarif.resolve()
    repository = args.repository.resolve()
    sarif = json.loads(sarif_path.read_text(encoding="utf-8"))

    updated, skipped, groups = stabilize(sarif, repository, verbose=args.verbose)

    print(f"Fingerprinted {updated} Clippy results ({FINGERPRINT_VERSION}); skipped {skipped}.")
    if args.stats:
        collided = {k: v for k, v in groups.items() if len(v) > 1}
        print(f"  distinct identities : {len(groups)}")
        print(f"  ordinal-disambiguated groups: {len(collided)}")
        for key, records in sorted(collided.items(), key=lambda kv: -len(kv[1]))[:10]:
            path, chain, _ = key
            print(f"    x{len(records):<3} {path}  {chain[:46]}  {records[0][2]['span'][:36]!r}")

    if skipped and args.fail_on_skipped:
        # Leave the report exactly as we found it. A half-fingerprinted SARIF is
        # worse than a failed step: the upload would succeed, and every result we
        # could not key would silently fall back to GitHub's own line hash - a
        # fresh alert with no dismissal history, anchored to a line that moves
        # again on the next edit.
        print(
            f"error: {skipped} of {updated + skipped} results were not fingerprinted; "
            f"refusing to write a partial report to {sarif_path}",
            file=sys.stderr,
        )
        return 1

    sarif_path.write_text(json.dumps(sarif, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
