"""Brace-accurate Rust item scanner: comments, strings, raw strings, lifetimes.

This is deliberately not a Rust parser. It is a lexical scanner that is correct
about the things that break naive regex/brace counting: line and (nested) block
comments, every prefixed string form (``b".."``, ``c".."``, ``r#".."#``,
``br#".."#``, ``cr#".."#``), char literals, and the lifetime-vs-char-literal
ambiguity. That is enough to build an accurate chain of enclosing items for any
byte offset, which is what finding identity needs.

What it is deliberately *not* correct about is listed under "Known latent
limitations" below.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Known latent limitations
# ---------------------------------------------------------------------------
# Four constructs this scanner reads wrongly. None of them occurs in this
# repository. For 2, 3 and 4, as of this commit
#
#   grep -rnE '<[[:space:]]*const[[:space:]]|\bwhere[A-Za-z0-9_]|r#[A-Za-z_]' --include='*.rs' src/
#
# exits 1 with no output across the 21 `.rs` files under `src/`; 1 needs a
# brace-matched sweep rather than a grep and carries its own count below. `src/`
# is a superset of the corpus - all 177 findings in target/inline-clippy.sarif
# sit in 15 files, every one of them under `src/`.
#
# Each was fixed once and each fix was reverted. Every finding's identity is
# derived from this lexer, so a change here re-keys whatever it touches; that
# price is worth paying for a defect the corpus actually contains and is not
# worth paying for one it does not. Limitation 1's fix charged the price twice
# over - it introduced a *new* instability, measurable on constructs this
# repository does use - and 2 to 4 bought nothing the grep above can find.
#
#   1. A one-element tuple normalizes like its parenthesised form. `(a,)` and
#      `(a)` both normalize to `(a)`; `f((x,))` and `f((x))` both to `f((x))`.
#      Keeping that comma means deciding, for every `(`, whether it opens a call
#      or a tuple - and a `(` judged wrongly turns a rustfmt reflow into a moved
#      fingerprint. The arithmetic is one-sided. Sweeping `src/` for the shape
#      `( X , )` finds 8 of them, and every one is a single-argument call or a
#      parameter list that rustfmt reflowed onto its own line; one-element
#      tuples: 0. Four of the 8 enclose a corpus finding. The rule would
#      therefore have to judge 8 real constructs in order to tell apart 0 real
#      ones, with a live fingerprint riding on half of those judgements.
#      Callees spelled like keywords are where such a rule goes wrong: `union`
#      and `fn` are both in this module's own ITEM_KEYWORDS, yet both are legal
#      callees - the method call `a.union(b)`, the function-pointer type
#      `fn(T)` - as are raw identifiers such as `r#type(x)`. Dropping the comma
#      unconditionally leaves all of those invariant under a reflow.
#      See `_drop_trailing_commas`.
#   2. A const generic parameter overwrites the pending item, taking the item it
#      parameterises with it: `pub struct Buf<const N: usize>;` scans to the
#      single item `const N`, and `impl<const N: usize> Buf<N> { fn a() {} }` to
#      `const N > fn a`. Suppressing `const` while a signature is open fixes
#      both but not the sibling case `fn f() { let p = q as *const u8; }`, which
#      scans to `fn f` plus a phantom `const u8` with or without that guard,
#      because no signature is open inside a body. See the `const`/`static`
#      branch of `scan_items`.
#   3. A path segment that merely starts with `where` truncates an impl label:
#      `impl Display for wherever::Thing {}` labels as `impl Display for`. A
#      real where-clause is terminated correctly - `impl Display for Thing where
#      Thing: Clone {}` labels as `impl Display for Thing`. See `_impl_label`.
#   4. A raw identifier's keyword half is read as a keyword, because `#` is not
#      an IDENT character and so does not break the word boundary. In
#      `fn real() { let r#impl = 1; let y = 2; }` followed by `impl A for B {}`,
#      the `impl` of `r#impl` opens a phantom item whose label is the text
#      swallowed up to the next brace. See `_word_at`.
#
# The symptom to watch for, if one ever does occur, is an `item_chain` naming an
# item that is not in the source (2 and 4) or stopping short of the type (3), or
# a fingerprint that moves across a pure rustfmt run (1).

BACKSLASH = chr(92)
IDENT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
ITEM_KEYWORDS = (
    "fn", "struct", "enum", "trait", "impl", "mod", "union",
    # bodiless item forms: `pub struct Unit;`, `const MAX: u32 = 8;`,
    # `type Alias = ...;`, `static TABLE: [u8; 4] = ...;`
    "type", "const", "static",
)


def _skip_string(src: str, i: int) -> int:
    """At the opening quote of a normal string. Returns index past the literal."""
    j = i + 1
    while j < len(src):
        if src[j] == BACKSLASH:
            j += 2
            continue
        if src[j] == '"':
            return j + 1
        j += 1
    return len(src)


def _skip_char_or_lifetime(src: str, i: int) -> int:
    """A "'" starts either a char literal or a lifetime. Distinguish them.

    The lifetime name is consumed along with the quote. Leaving it as code would
    expose `'static` as a bare `static` keyword, which the item scanner would
    then read as a `static` item declaration - producing a phantom item named
    `static str` out of `&'static str`.
    """
    j = i + 1
    if j < len(src) and src[j] == BACKSLASH:
        # Step over the escaped character before hunting for the closing quote.
        # Starting at j + 1 lands *on* the escaped quote of an escaped-quote
        # literal, so `'\''` ended after three bytes and was split into a token
        # plus a dangling quote.
        k = j + 2
        while k < len(src) and src[k] != "'":
            k += 1
        return min(k + 1, len(src))
    if j + 1 < len(src) and src[j + 1] == "'":
        return j + 2
    k = j
    while k < len(src) and src[k] in IDENT:
        k += 1
    return max(k, i + 1)


# The characters that can open a literal: a quote, or one of the prefix letters
# Rust allows before one - `b`, `c`, `r`.
_LITERAL_STARTS = frozenset("bcr" + chr(34) + chr(39))


def _literal_end(src: str, i: int, limit=None):
    """``(kind, end)`` for the literal beginning at ``i``, else ``None``.

    ``kind`` is ``"lit"`` for the double-quoted family - ``".."``, ``b".."``,
    ``c".."``, ``r".."``, ``r#".."#``, ``br#".."#``, ``cr#".."#`` - and
    ``"char"`` for a char literal (``'x'``, ``b'x'``) or a lifetime. ``end`` is
    the index just past the literal.

    Both :func:`masks` and :func:`_tokenize` route every literal through here, so
    that they cannot disagree about where one ends. A literal the scanner stops
    short of is not a cosmetic error: :func:`masks` carries on lexing the
    literal's own text as code, and the live code after it is dropped from the
    identity.

    Every prefix the double-quoted family allows is recognised here, ``c`` (C
    strings; this crate is ``edition = "2024"``) included. The identifier guard
    covers the whole prefix rather than ``r`` alone, and applies only where there
    is a prefix; a bare ``"`` never needed one. Delete ``c`` from
    :data:`_LITERAL_STARTS` and both ``cr#"a " b"#; let live = 1;`` and
    ``cr"C:\\"; let live = 1;`` put every byte of ``let live`` outside the code
    mask - the first because the scan reaches the ``r``, sees ``c`` before it and
    refuses the raw string, the second because it then reads a backslash that a
    raw string does not have as an escape.

    ``limit`` bounds where the opening delimiter may start, so tokenizing a
    sub-span cannot reach past its end to decide that a trailing ``r`` opens a
    raw string.
    """
    n = len(src)
    limit = n if limit is None else min(limit, n)
    if src[i] == "'":
        return "char", _skip_char_or_lifetime(src, i)
    j = i
    byte = src[j] == "b"
    cstr = src[j] == "c"
    if byte:
        j += 1
        if j < limit and src[j] == "'":
            if i and src[i - 1] in IDENT:
                return None
            return "char", _skip_char_or_lifetime(src, j)
    elif cstr:
        # A C string. There is no `c'x'` form, so `c` never opens a char
        # literal, and `bc".."` is not a prefix, so `c` never follows `b`.
        j += 1
    hashes = 0
    raw = j < limit and src[j] == "r"
    if raw:
        j += 1
        while j < limit and src[j] == "#":
            hashes += 1
            j += 1
    if j >= limit or src[j] != '"':
        # Not a literal: a bare word such as `bar`, or the raw *identifier*
        # `r#impl`, where the `#` run is followed by a name instead of a quote.
        return None
    if (byte or cstr or raw) and i and src[i - 1] in IDENT:
        return None
    if not raw:
        return "lit", _skip_string(src, j)
    # A raw string has no escapes: it ends at the first quote followed by as many
    # `#` as opened it. `_skip_string` would read the backslash in `br"C:\"` as
    # an escape and run past the end.
    j += 1
    close = '"' + "#" * hashes
    k = src.find(close, j)
    return "lit", (n if k < 0 else k + len(close))


def masks(src: str):
    """Return (code, comment) masks.

    code[i]    == 1 where byte i is real code (not a comment, not inside a literal)
    comment[i] == 1 where byte i is inside a line or block comment

    They are not complements: string-literal bytes are 0 in both, because a
    literal's content is semantically significant to a finding's identity while a
    comment's content is not.
    """
    mask = bytearray(len(src))
    comment = bytearray(len(src))
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                comment[k] = 1
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if src[j] == "/" and j + 1 < n and src[j + 1] == "*":
                    depth += 1
                    j += 2
                    continue
                if src[j] == "*" and j + 1 < n and src[j + 1] == "/":
                    depth -= 1
                    j += 2
                    continue
                j += 1
            for k in range(i, min(j, n)):
                comment[k] = 1
            i = j
            continue
        if c in _LITERAL_STARTS:
            found = _literal_end(src, i)
            if found is not None:
                i = max(found[1], i + 1)
                continue
        mask[i] = 1
        i += 1
    return mask, comment


def code_mask(src: str) -> bytearray:
    """1 where the byte is real code, 0 inside a comment or string literal."""
    return masks(src)[0]


def comment_mask(src: str) -> bytearray:
    """1 where the byte is inside a line or block comment."""
    return masks(src)[1]


# Two adjacent tokens only need a separator when dropping it would fuse them
# into one token. Everything else is joined tight, so reflowing whitespace is
# invisible to identity.
_GLUED = ("word", "lit")
_TRAILING_COMMA_BEFORE = (")", "]", "}")


def _tokenize(src: str, lo: int, hi: int, comments):
    """Lex src[lo:hi] into (kind, text) pairs; kind is "word", "lit" or "punct"."""
    out = []
    # Offsets arrive from SARIF, so no entry point here may raise on one that is
    # out of range. Without these two lines `normalize_tokens(src, 0, len(src) +
    # 1)` raises IndexError, and a negative `lo` indexes backwards from the end:
    # `normalize_tokens("fn a() {}", -4, 9)` returns "){}fn a(){}".
    hi = min(hi, len(src))
    i = max(lo, 0)
    while i < hi:
        c = src[i]
        if comments is not None and i < len(comments) and comments[i]:
            i += 1
            continue
        if c in " \t\r\n":
            i += 1
            continue
        if c in _LITERAL_STARTS:
            found = _literal_end(src, i, hi)
            if found is not None:
                kind, j = found
                # A char literal or a lifetime stays a "word": `'static` must
                # keep a separator from whatever identifier follows it.
                out.append(("lit" if kind == "lit" else "word", src[i : min(j, hi)]))
                i = max(j, i + 1)
                continue
        if c in IDENT:
            j = i
            while j < hi and src[j] in IDENT:
                j += 1
            out.append(("word", src[i:j]))
            i = j
            continue
        out.append(("punct", c))
        i += 1
    return out


def _drop_trailing_commas(tokens):
    """Remove comma runs that sit just before a closer, or at the very end.

    rustfmt adds a trailing comma whenever it reflows a call or a literal across
    several lines. That is pure formatting, so it must not change identity.

    Every such comma goes, including the one that makes ``(a,)`` a one-element
    tuple rather than a parenthesised ``a``. Limitation 1 at the top of this
    module records why that case is left alone.
    """
    drop = set()
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i] == ("punct", ","):
            j = i
            while j < n and tokens[j] == ("punct", ","):
                j += 1
            if j == n or (tokens[j][0] == "punct" and tokens[j][1] in _TRAILING_COMMA_BEFORE):
                drop.update(range(i, j))
            i = j
            continue
        i += 1
    return [t for k, t in enumerate(tokens) if k not in drop]


def normalize_tokens(src: str, lo: int, hi: int, comments=None) -> str:
    """Token-normalized form of src[lo:hi], for comparing two spans of code.

    Whitespace between tokens, and the trailing commas rustfmt inserts when it
    reflows across lines, are formatting rather than identity - two spellings of
    the same code must normalize to the same string. String, byte-string and raw
    string literals are the exception: they are emitted verbatim, so ``"a  b"``
    and ``"a b"`` stay distinct.

    ``comments`` is the comment mask from :func:`masks`; when it is given those
    bytes are skipped. Passing ``None`` tokenizes comment text like any other
    text, which is what a caller wants for a span that is *only* a comment.
    """
    tokens = _drop_trailing_commas(_tokenize(src, lo, hi, comments))
    parts = []
    previous = None
    for kind, text in tokens:
        if previous in _GLUED and kind in _GLUED:
            parts.append(" ")
        parts.append(text)
        previous = kind
    return "".join(parts)


def _word_at(src: str, mask: bytearray, i: int):
    if not mask[i] or (i and src[i - 1] in IDENT):
        return None
    j = i
    while j < len(src) and src[j] in IDENT and mask[j]:
        j += 1
    return src[i:j] if j > i else None


def _ident_after(src: str, mask: bytearray, i: int):
    j = i
    while j < len(src) and (not mask[j] or src[j] in " \t\r\n"):
        j += 1
    k = j
    while k < len(src) and src[k] in IDENT:
        k += 1
    return src[j:k], k


def _impl_label(src: str, mask: bytearray, i: int) -> str:
    """Capture 'impl Trait<Args> for Type<Args>'.

    Generic *arguments* are kept, because they are what distinguishes sibling
    impls: this repository has ``impl From<&str> for StepError``,
    ``impl From<String> for StepError`` and ``impl From<aes_gcm::Error> for
    StepError`` side by side, each with its own ``fn from``. Dropping the
    arguments would merge all three.

    The generic *parameter* list immediately after ``impl`` is dropped. What that
    buys is that the **bounds** stay out of identity: widening
    ``impl<T: Clone>`` to ``impl<T: Clone + Send>`` leaves every finding in the
    block alone. It does **not** make a parameter rename free. The parameter
    almost always reappears in argument position, and there it is kept, so
    ``impl<T: Clone> Wrap<T>`` and ``impl<U: Clone> Wrap<U>`` label as
    ``impl Wrap<T>`` and ``impl Wrap<U>``, giving chains ``impl Wrap<T> > fn g``
    and ``impl Wrap<U> > fn g`` - a rename of the parameter does re-key every
    finding in the block. Only a parameter that appears nowhere in the impl's
    *type* is genuinely free to rename.
    """
    n = len(src)
    j = i + 4

    # Skip the generic parameter list, if any: impl<T: Clone> ...
    k = j
    while k < n and (not mask[k] or src[k] in " \t\r\n"):
        k += 1
    if k < n and mask[k] and src[k] == "<":
        depth = 0
        while k < n:
            if mask[k]:
                if src[k] == "<":
                    depth += 1
                elif src[k] == ">":
                    depth -= 1
                    if depth == 0:
                        k += 1
                        break
            k += 1
        j = k

    depth = 0
    buf = []
    while j < n:
        if mask[j]:
            ch = src[j]
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth = max(0, depth - 1)
            elif ch == "{" and depth == 0:
                break
            elif depth == 0 and src[j : j + 5] == "where" and src[j - 1] not in IDENT:
                # A where-clause is a bound, not part of the label. Limitation 3
                # at the top of this module records the path that merely starts
                # with those five letters.
                break
            buf.append(ch)
        j += 1
    return " ".join("".join(buf).split())


MODIFIERS = ("pub", "async", "unsafe", "const", "extern", "default", "static")


def _attached_comment_start(src: str, comments: bytearray, position: int) -> int:
    """Start of the comment block attached to the header at ``position``.

    Returns ``position`` unchanged when nothing is attached.

    A ``///`` or ``/** */`` block directly above an item is part of that item's
    header, and doc lints report spans *inside* it: this repository has 22
    ``clippy::missing_errors_doc``, 10 ``clippy::missing_panics_doc`` and 4
    ``clippy::doc_markdown``. Leaving the comment outside the item put those
    findings outside every item, so ``item_chain`` fell through to
    ``<file-scope>`` and - far worse - :func:`enclosing_context` missed its
    ``<hdr>`` guard and took the documented function's own signature and body as
    the context instead: with the guard suppressed all 36 of those findings get a
    body window, 12 of them long enough to hit :data:`CONTEXT_CAP`. Editing a
    statement in the body then re-keyed the doc finding, which is what the
    ``<hdr>`` guard exists to prevent.

    Two limits keep the walk from claiming text that is not a header:

    * the comment must own its line, so the trailing ``// why`` on
      ``let x = 1; // why`` stays with that statement rather than joining the
      next item;
    * the walk stops at a blank line, and unconditionally at an inner doc
      comment (``//!``, ``/*!``), so a file's module header is not swallowed by
      whichever item happens to follow it - an inner doc documents the module it
      sits in, never the next item, blank line or no blank line.
    """
    j = position - 1
    newlines = 0
    while j >= 0 and src[j] in " \t\r\n":
        if src[j] == "\n":
            newlines += 1
            if newlines > 1:
                return position
        j -= 1
    if j < 0 or not comments[j]:
        return position
    start = j
    while start >= 0 and comments[start]:
        start -= 1
    start += 1
    line_start = src.rfind("\n", 0, start) + 1
    if src[line_start:start].strip():
        return position
    if src[start : start + 3] in ("//!", "/*!"):
        return position
    return start


def _extend_header_start(src: str, mask: bytearray, comments: bytearray, i: int) -> int:
    """Walk back from an item keyword over its modifiers, attributes and docs.

    A lint that fires on a declaration (``clippy::missing_errors_doc``,
    ``clippy::large_enum_variant``) gets a span starting at ``pub``, which sits
    before the ``fn``/``enum`` keyword, or inside the doc comment above it.
    Anchoring the item at its keyword would put those findings outside their own
    item and collapse them to file scope.
    """
    position = i
    while True:
        # A doc-comment block above the header, possibly interleaved with the
        # attributes handled below: `/// docs`, then `#[inline]`, then `pub fn`.
        attached = _attached_comment_start(src, comments, position)
        if attached != position:
            position = attached
            continue

        j = position - 1
        while j >= 0 and (not mask[j] or src[j] in " \t\r\n"):
            j -= 1
        if j < 0:
            return position

        # pub(crate) / pub(super) / extern "C"
        if mask[j] and src[j] == ")":
            depth = 0
            k = j
            while k >= 0:
                if mask[k]:
                    if src[k] == ")":
                        depth += 1
                    elif src[k] == "(":
                        depth -= 1
                        if depth == 0:
                            break
                k -= 1
            if k < 0:
                return position
            j = k - 1
            while j >= 0 and (not mask[j] or src[j] in " \t\r\n"):
                j -= 1

        # an attribute group: #[...] or #![...]
        if j >= 0 and not mask[j] and src[j] == "]":
            depth = 0
            k = j
            while k >= 0:
                if src[k] == "]":
                    depth += 1
                elif src[k] == "[":
                    depth -= 1
                    if depth == 0:
                        break
                k -= 1
            if k > 0 and src[k - 1] == "#":
                position = k - 1
                continue
            return position
        if j >= 0 and src[j] == "]":
            depth = 0
            k = j
            while k >= 0:
                if src[k] == "]":
                    depth += 1
                elif src[k] == "[":
                    depth -= 1
                    if depth == 0:
                        break
                k -= 1
            start = k - 1
            if start >= 0 and src[start] == "!":
                start -= 1
            if start >= 0 and src[start] == "#":
                position = start
                continue
            return position

        # a modifier keyword, or a string literal following `extern`
        end = j + 1
        while j >= 0 and src[j] in IDENT and mask[j]:
            j -= 1
        word = src[j + 1 : end]
        if word in MODIFIERS:
            position = j + 1
            continue
        return position


def scan_items(src: str):
    """Return [{kind, name, label, start, body_start, body_end}] for every braced item."""
    mask, comments = masks(src)
    n = len(src)
    items = []
    stack = []
    pending = None
    sig_depth = 0  # () and [] nesting inside a signature we have not closed yet
    i = 0
    while i < n:
        ch = src[i]
        if mask[i]:
            word = _word_at(src, mask, i)
            if word in ITEM_KEYWORDS:
                # `const fn` / `const unsafe fn` is a modifier, not a const
                # item. Let the `fn` handler claim it; _extend_header_start
                # pulls the `const` back into the header.
                if word in ("const", "static"):
                    peek, _ = _ident_after(src, mask, i + len(word))
                    if peek in ("fn", "unsafe", "async", "extern"):
                        i += len(word)
                        continue
                if word == "impl":
                    # 'impl' in type position ('-> impl Iterator', 'x: impl Into<T>')
                    # is not an item. We are in type position exactly when
                    # another item's signature is still open.
                    if pending is not None:
                        i += 4
                        continue
                    label = _impl_label(src, mask, i)
                    pending = {
                        "kind": "impl",
                        "name": label,
                        "label": ("impl " + label) if label else "impl",
                        "start": _extend_header_start(src, mask, comments, i),
                    }
                    sig_depth = 0
                    i += 4
                    continue
                name, k = _ident_after(src, mask, i + len(word))
                if name:
                    pending = {
                        "kind": word,
                        "name": name,
                        "label": word + " " + name,
                        "start": _extend_header_start(src, mask, comments, i),
                    }
                    sig_depth = 0
                    i = k
                    continue
            if ch in "([":
                if pending is not None:
                    sig_depth += 1
            elif ch in ")]":
                if pending is not None and sig_depth:
                    sig_depth -= 1
            elif ch == "{":
                if pending is not None:
                    pending["body_start"] = i + 1
                    pending["header_end"] = i + 1
                    stack.append((i, pending))
                    pending = None
                    sig_depth = 0
                else:
                    stack.append((i, None))
            elif ch == "}":
                if stack:
                    _, item = stack.pop()
                    if item is not None:
                        item["body_end"] = i
                        items.append(item)
            elif ch == ";":
                # A ';' ends a bodiless declaration, but one inside `[u8; N]` in
                # a signature does not. Bodiless items are still items: a lint on
                # `pub struct MacKey;` or on a `const` must anchor to it rather
                # than fall through to file scope, so record it with its header
                # as its range.
                if pending is not None and sig_depth == 0:
                    pending["body_start"] = pending["start"]
                    pending["body_end"] = i + 1
                    pending["header_end"] = i + 1
                    items.append(pending)
                    pending = None
        i += 1
    for _, item in stack:
        if item is not None:
            item.setdefault("header_end", item["body_start"])
            item["body_end"] = n
            items.append(item)
    items.sort(key=lambda d: d["body_start"])
    return items


OPENERS = "([{"
CLOSERS = ")]}"
CONTEXT_CAP = 300
# The arm pattern is a prefix; cap it separately so it can never crowd out the
# statement that follows it, which is what separates siblings inside one arm.
ARM_CAP = 120


def _innermost(items, offset: int):
    enclosing = [it for it in items if it["start"] <= offset < it["body_end"]]
    if not enclosing:
        return None
    return max(enclosing, key=lambda d: d["start"])


def _enclosing_open(src: str, mask: bytearray, offset: int) -> int:
    """Index of the innermost delimiter still open at `offset`, or -1."""
    depth = 0
    j = offset - 1
    while j >= 0:
        if mask[j]:
            if src[j] in CLOSERS:
                depth += 1
            elif src[j] in OPENERS:
                if depth == 0:
                    return j
                depth -= 1
        j -= 1
    return -1


def _next_code(src: str, mask: bytearray, j: int) -> str:
    while j < len(src):
        if mask[j] and src[j] not in " \t\r\n":
            return src[j]
        j += 1
    return ""


def _is_boundary(src: str, mask: bytearray, j: int) -> bool:
    if src[j] in ";,":
        return True
    if src[j] == "}":
        # A block ends a statement unless it is being chained off: `match x {..}.foo()`
        return _next_code(src, mask, j + 1) not in (".", "?")
    return False


def _strip(src: str, comments, lo: int, hi: int) -> str:
    """Normalize src[lo:hi] for identity, dropping comment bytes.

    A doc or explanatory comment sitting at the head of a statement or match arm
    would otherwise be part of the identity, so rewording it would close the
    alert and open a new one.

    This delegates to :func:`normalize_tokens` (note the argument order differs)
    so that the span tier and the context tier can never disagree about what a
    formatting-only change looks like.
    """
    return normalize_tokens(src, lo, hi, comments)


def enclosing_context(src: str, mask: bytearray, items, offset: int, comments=None) -> str:
    """The smallest enclosing statement, match arm, or argument around `offset`.

    This is what separates findings that are textually identical. The four `|_|`
    closures in `fn parse_smt_header` are indistinguishable as spans, but their
    statements carry "Bad KEM ciphertext length", "Bad nonce_prefix length",
    "Bad file_len" and "Bad chunk_size" - so they need no positional counter, and
    deleting one of them leaves the others byte-identical.

    Findings on an item's header return the literal ``<hdr>`` instead.
    :func:`item_chain` already identifies those exactly, and a window around a signature
    would run into the function body and import all of its churn, re-keying a
    header finding whenever an unrelated statement below it changes.

    The header runs from the start of the item's attached doc comment (see
    :func:`_attached_comment_start`) to its opening brace, so a doc lint firing
    *inside* the comment is covered too. It was not, and those findings picked up
    the whole following signature and body as their context - the exact failure
    this guard is here to prevent.
    """
    # Both scans below index `mask` from `offset`. Drop this line and an offset
    # past the end raises IndexError; a negative one indexes backwards from the
    # end instead of clamping to the start. Clamp once here, as
    # :func:`normalize_tokens` clamps its own bounds. `item_chain` needs no
    # clamp - it only compares offsets, never indexes with one.
    offset = min(max(offset, 0), len(src))

    item = _innermost(items, offset)
    if item is not None and offset < item.get("header_end", item["body_start"]):
        return "<hdr>"

    own_open = _enclosing_open(src, mask, offset)
    start = own_open + 1
    stop = len(src)
    if own_open >= 0:
        depth = 0
        j = own_open
        while j < len(src):
            if mask[j]:
                if src[j] in OPENERS:
                    depth += 1
                elif src[j] in CLOSERS:
                    depth -= 1
                    if depth == 0:
                        stop = j
                        break
            j += 1

    depth = 0
    lo = start
    j = offset - 1
    while j >= start:
        if mask[j]:
            if src[j] in CLOSERS and src[j] != "}":
                depth += 1
            elif src[j] in OPENERS:
                depth = max(0, depth - 1)
            elif depth == 0 and _is_boundary(src, mask, j):
                lo = j + 1
                break
            elif src[j] == "}":
                depth += 1
        j -= 1

    depth = 0
    hi = stop
    j = offset
    while j < stop:
        if mask[j]:
            if src[j] in OPENERS:
                depth += 1
            elif src[j] in CLOSERS:
                if depth == 0:
                    hi = j
                    break
                depth -= 1
                if depth == 0 and src[j] == "}" and _is_boundary(src, mask, j):
                    hi = j + 1
                    break
            elif depth == 0 and src[j] in ";,":
                hi = j
                break
        j += 1

    text = _strip(src, comments, lo, hi)

    # A match arm with a block body scopes the segment to the block, leaving the
    # discriminating pattern outside it. Prepend the pattern.
    if own_open >= 0 and src[own_open] == "{":
        j = own_open - 1
        while j >= 0 and (not mask[j] or src[j] in " \t\r\n"):
            j -= 1
        if j >= 1 and mask[j] and src[j] == ">" and src[j - 1] == "=":
            arm_end = j - 1
            depth = 0
            k = arm_end - 1
            arm_lo = 0
            while k >= 0:
                if mask[k]:
                    ch = src[k]
                    if ch in CLOSERS:
                        # A previous arm's block closes here. Without this the
                        # scan balances straight over it and keeps going, so the
                        # "pattern" ends up containing every earlier arm.
                        if depth == 0 and ch == "}":
                            arm_lo = k + 1
                            break
                        depth += 1
                    elif ch in OPENERS:
                        if depth == 0:
                            arm_lo = k + 1
                            break
                        depth -= 1
                    elif depth == 0 and ch in ";,":
                        arm_lo = k + 1
                        break
                k -= 1
            pattern = _strip(src, comments, arm_lo, arm_end)[:ARM_CAP]
            text = pattern + " => " + text

    return text[:CONTEXT_CAP]


def item_chain(items, offset: int) -> str:
    """The chain of enclosing items for a byte offset, outermost first.

    Containment starts at the item's declaration keyword, not at its opening
    brace, so a lint that fires on a signature (``clippy::missing_errors_doc``,
    ``clippy::must_use_candidate``) anchors to the item it describes rather than
    falling through to file scope.
    """
    enclosing = [it for it in items if it["start"] <= offset < it["body_end"]]
    if not enclosing:
        return "<file-scope>"
    enclosing.sort(key=lambda d: d["start"])
    return " > ".join(it["label"] for it in enclosing)
