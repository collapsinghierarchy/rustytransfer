#!/usr/bin/env python3
"""Unit tests for the lexical scanner that finding identity is built on.

``test_identity.py`` exercises the scanner only end to end, through a whole
SARIF corpus::

    python3 .github/scripts/tests/test_identity.py target/inline-clippy.sarif .

and that is too coarse a net: ``-> impl Trait`` was parsed as an item
declaration for months because no finding in this repository happened to sit
inside such a function, so every fingerprint still matched itself and the
harness stayed green. The same hole swallowed ``br".."`` (torn into ``b`` plus a
raw string, so a ``//`` inside such a literal masked the rest of the line's real
code out of existence) and ``r#impl`` (read as the ``impl`` keyword, inventing a
whole phantom item) - neither construct occurs in this repository's 177
findings, so both could have stayed broken indefinitely behind a green corpus.

These tests pin the scanner's behaviour directly, one construct at a time, so a
regression names the construct it broke, whether or not the corpus happens to
contain an example. Where a known limitation was deliberately *not* fixed, the
current behaviour is pinned too, with the reasoning, so that it cannot change
silently in either direction.

Plain python on purpose - pytest is not a dependency of this repository, and CI
runs this file with nothing installed:

    python3 .github/scripts/tests/test_rustscan.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rustscan import (  # noqa: E402
    enclosing_context,
    item_chain,
    masks,
    normalize_tokens,
    scan_items,
)

# Spelling a backslash as a literal in a test that is *about* backslash handling
# invites an escaping mistake in the test itself to be mistaken for a scanner
# bug. rustscan.py names it for the same reason.
BACKSLASH = chr(92)
QUOTE = chr(34)


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

CASES = []


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


class Expect:
    """Collects every failure in a case instead of stopping at the first.

    One construct usually breaks several assertions at once (an item's label,
    its span and its chain), and seeing all of them together is what tells you
    whether the scanner mis-parsed or merely mis-labelled.
    """

    def __init__(self):
        self.failures = []

    def equal(self, actual, expected, what):
        if actual != expected:
            self.failures.append(
                f"{what}\n"
                f"             expected: {expected!r}\n"
                f"             actual:   {actual!r}"
            )

    def differ(self, left, right, what):
        if left == right:
            self.failures.append(f"{what}\n             both were: {left!r}")


def labels(src: str):
    return [item["label"] for item in scan_items(src)]


def starts(src: str):
    return {item["label"]: item["start"] for item in scan_items(src)}


def norm(src: str, comments=None) -> str:
    return normalize_tokens(src, 0, len(src), comments)


def chain_at(src: str, needle: str, start: int = 0) -> str:
    return item_chain(scan_items(src), src.index(needle, start))


def context_at(src: str, needle: str, start: int = 0) -> str:
    """`enclosing_context` at the first occurrence of `needle`, masks and all."""
    code, comment = masks(src)
    offset = src.index(needle, start)
    return enclosing_context(src, code, scan_items(src), offset, comment)


def masked_as_comment(src: str, needle: str) -> bool:
    _code, comment = masks(src)
    return bool(comment[src.index(needle)])


# --------------------------------------------------------------------------
# scan_items: `impl` in type position is not an item
# --------------------------------------------------------------------------

@case("`-> impl Trait` is a return type, not an impl block")
def _impl_return(e):
    src = "fn make() -> impl Iterator<Item = u32> {\n    (0..3).into_iter()\n}\n"
    e.equal(labels(src), ["fn make"], "the return type opened a phantom impl item")
    e.equal(chain_at(src, "into_iter"), "fn make", "body fell into the wrong item")


@case("`x: impl Into<T>` is a parameter type, not an impl block")
def _impl_argument(e):
    src = "pub fn take(x: impl Into<String>) -> String {\n    x.into()\n}\n"
    e.equal(labels(src), ["fn take"], "the parameter type opened a phantom impl item")
    e.equal(chain_at(src, "x.into"), "fn take", "body fell into the wrong item")


@case("a real impl block at file scope is still an item")
def _impl_real(e):
    src = "impl Local {\n    fn inner(&self) {}\n}\n"
    e.equal(labels(src), ["impl Local", "fn inner"], "a top-level impl was skipped")


# --------------------------------------------------------------------------
# scan_items: impl labelling
# --------------------------------------------------------------------------

SIBLING_IMPLS = (
    "impl From<&str> for StepError {\n"
    "    fn from(v: &str) -> Self { StepError::Bad }\n"
    "}\n"
    "\n"
    "impl From<String> for StepError {\n"
    "    fn from(v: String) -> Self { StepError::Bad }\n"
    "}\n"
)


@case("sibling `impl From<X> for Y` blocks keep their generic arguments")
def _sibling_impls(e):
    e.equal(
        labels(SIBLING_IMPLS),
        ["impl From<&str> for StepError", "fn from",
         "impl From<String> for StepError", "fn from"],
        "generic arguments were dropped, merging the two impls",
    )
    first = chain_at(SIBLING_IMPLS, "StepError::Bad")
    second = chain_at(SIBLING_IMPLS, "StepError::Bad",
                      SIBLING_IMPLS.index("String>"))
    e.equal(first, "impl From<&str> for StepError > fn from", "first `fn from`")
    e.equal(second, "impl From<String> for StepError > fn from", "second `fn from`")
    e.differ(first, second, "the two `fn from` bodies share one identity")


@case("the generic PARAMETER list after `impl` is dropped, arguments are kept")
def _impl_generics(e):
    src = ("impl<T: Clone> From<Vec<T>> for Bag {\n"
           "    fn from(v: Vec<T>) -> Self { Bag }\n"
           "}\n")
    e.equal(labels(src), ["impl From<Vec<T>> for Bag", "fn from"],
            "the bounds on a type parameter must not be part of identity")


@case("LATENT 3: a `where`-prefixed path truncates the impl label")
def _impl_where_prefix(e):
    # Pins limitation 3 in rustscan.py's header. The `where` terminator checks a
    # word boundary only on its LEADING side, so `wherever::Thing` matches and
    # the implementing type is dropped. Not fixed: no `\bwhere[A-Za-z0-9_]` match
    # exists anywhere under src/, and every identity is derived from this lexer,
    # so touching it re-keys findings to buy nothing the corpus contains.
    # This asserts the CURRENT behaviour so it cannot change unnoticed.
    e.equal(labels("impl Display for wherever::Thing {}"),
            ["impl Display for"],
            "the documented truncation changed -- update limitation 3")
    # What must keep working: a real where-clause still terminates the label.
    e.equal(labels("impl Display for Thing where Thing: Clone {}"),
            ["impl Display for Thing"],
            "a real where-clause must still terminate the label")
    e.equal(labels("impl<T> Display for Wrap<T> where T: Clone {}"),
            ["impl Display for Wrap<T>"],
            "a real where-clause after generic arguments")


# --------------------------------------------------------------------------
# scan_items: const/static in a position that is not an item
# --------------------------------------------------------------------------

@case("LATENT 2: a const generic parameter overwrites the item it parameterises")
def _const_generic(e):
    src = ("pub struct Buf<const N: usize>;\n"
           "impl<const N: usize> Buf<N> {\n"
           "    fn len(&self) -> usize { N }\n"
           "}\n")
    # Pins limitation 2. `const N` inside a still-open signature overwrites the
    # pending item, so the struct and the impl are both lost and the two
    # surviving items are BOTH labelled `const N`. Not fixed: no `<const ` match
    # exists under src/, and the obvious fix (suppress `const` while a signature
    # is open) misses `let p = q as *const u8;` in a fn body, where nothing is
    # pending. This asserts the CURRENT behaviour so it cannot change unnoticed.
    e.equal(labels(src), ["const N", "const N", "fn len"],
            "the documented const-generic behaviour changed -- update limitation 2")
    e.equal(chain_at(src, "usize { N }"), "const N > fn len",
            "the documented const-generic chain changed -- update limitation 2")


@case("LATENT 4: a raw identifier's keyword half reads as the keyword")
def _raw_identifier(e):
    src = "fn real() { let r#impl = 1; let y = 2; }\nimpl A for B {}\n"
    # Pins limitation 4. `#` is not an identifier character, so the keyword half
    # of `r#impl` passes the word-boundary test and `_impl_label` swallows text
    # up to the next `{`, inventing a phantom item out of a `let`. Not fixed: no
    # `r#` identifier exists under src/.
    e.equal(labels(src),
            ["fn real", "impl = 1; let y = 2; } impl A for B", "impl A for B"],
            "the documented phantom-item behaviour changed -- update limitation 4")
    # The blast radius is bounded: surrounding code still chains to the real fn,
    # so the phantom label does not capture its siblings' identity.
    e.equal(chain_at(src, "let y"), "fn real",
            "the phantom item captured code after the raw identifier")
    # Only `r#` + an ITEM_KEYWORD misfires; a raw non-keyword is unaffected.
    e.equal(labels("fn real() { let r#match = 1; }\n"), ["fn real"],
            "`r#match` is not an item keyword and must not produce an item")


# --------------------------------------------------------------------------
# scan_items: nesting
# --------------------------------------------------------------------------

@case("an impl nested inside a fn body nests in the chain")
def _nested_impl(e):
    src = ("fn outer() {\n"
           "    struct Local;\n"
           "    impl Local {\n"
           "        fn inner(&self) {}\n"
           "    }\n"
           "}\n")
    e.equal(labels(src), ["fn outer", "struct Local", "impl Local", "fn inner"],
            "a nested item was lost")
    e.equal(chain_at(src, "fn inner"), "fn outer > impl Local > fn inner",
            "nesting was flattened")
    e.equal(chain_at(src, "struct Local"), "fn outer > struct Local",
            "a bodiless item inside a fn lost its parent")


# --------------------------------------------------------------------------
# scan_items: modifiers
# --------------------------------------------------------------------------

@case("`const fn` is a fn, and its header starts at `const`")
def _const_fn(e):
    src = "const fn small() -> u8 { 1 }\n"
    e.equal(labels(src), ["fn small"], "`const` was read as a const item")
    e.equal(scan_items(src)[0]["start"], 0,
            "a lint on the `const` keyword would fall out of its own item")


@case("`pub async unsafe fn` keeps every modifier inside the header")
def _modifier_stack(e):
    src = "mod m {\n    pub async unsafe fn risky() {}\n}\n"
    item = [i for i in scan_items(src) if i["label"] == "fn risky"]
    e.equal(len(item), 1, "the modifier stack hid the fn")
    if item:
        start = item[0]["start"]
        e.equal(src[start:start + 3], "pub",
                "the header did not extend back over `pub async unsafe`")
        e.equal(chain_at(src, "pub async"), "mod m > fn risky",
                "a lint on the signature fell through to the module")


# --------------------------------------------------------------------------
# scan_items: bodiless items
# --------------------------------------------------------------------------

@case("bodiless items (`pub struct MacKey;`, `const MAX: u32 = 8;`) are items")
def _bodiless(e):
    src = ("pub struct MacKey;\n"
           "const MAX: u32 = 8;\n"
           "static TABLE: [u8; 4] = [0; 4];\n"
           "type Alias = u32;\n")
    e.equal(labels(src),
            ["struct MacKey", "const MAX", "static TABLE", "type Alias"],
            "a declaration without a brace body was dropped")
    e.equal(chain_at(src, "MacKey"), "struct MacKey", "unit struct anchor")
    e.equal(chain_at(src, "MAX"), "const MAX", "const anchor")
    # The `;` inside `[u8; 4]` must not be mistaken for the end of the item.
    e.equal(chain_at(src, "[0; 4]"), "static TABLE",
            "the `;` inside an array type closed the item early")


# --------------------------------------------------------------------------
# masks: literals and comments must not be read as code
# --------------------------------------------------------------------------

@case('a raw string r#"..."# cannot open an item or a brace')
def _raw_string(e):
    src = 'fn real() {\n    let s = r#"fn ghost() {"#;\n    let t = "}";\n}\n'
    e.equal(labels(src), ["fn real"], "a literal produced a phantom item")
    e.equal(scan_items(src)[0]["body_end"], src.rindex("}"),
            "brace matching was thrown off by a literal")


@case("a byte string and a nested block comment cannot open an item or a brace")
def _byte_string_and_nested_comment(e):
    src = ('fn real() {\n'
           '    /* fn ghost() { /* nested */ still hidden */\n'
           '    let b = b"{{{";\n'
           '}\n')
    e.equal(labels(src), ["fn real"],
            "a comment or byte string produced a phantom item")
    e.equal(scan_items(src)[0]["body_end"], src.rindex("}"),
            "the nested block comment closed one level too early")
    code, comment = masks(src)
    ghost = src.index("fn ghost")
    e.equal(bool(comment[ghost]), True, "comment mask missed the outer comment")
    e.equal(bool(code[ghost]), False, "comment bytes were reported as code")
    braces = src.index('b"{{{"')
    e.equal(bool(code[braces + 2]), False,
            "byte-string bytes were reported as code")


@case("`'static` is a lifetime and `'x'` is a char - neither is code to scan")
def _lifetime_vs_char(e):
    src = "fn real(s: &'static str) -> char {\n    '{'\n}\n"
    e.equal(labels(src), ["fn real"],
            "`'static` was read as a `static` item declaration")
    e.equal(scan_items(src)[0]["body_end"], src.rindex("}"),
            "the `'{'` char literal opened a brace")
    src2 = 'struct S<\'a> { s: &\'a str }\nstatic Q: &\'static str = "q";\n'
    e.equal(labels(src2), ["struct S", "static Q"],
            "a lifetime parameter confused the item scanner")


# --------------------------------------------------------------------------
# masks: the raw BYTE string family, br".." / br#".."#
# --------------------------------------------------------------------------
#
# `masks` and `_tokenize` used to test for `r` and for `b"` separately and
# neither accepted `br`: the raw-string guard required the character before the
# `r` not to be an identifier character, and in `br".."` that character is `b`.
# The literal was torn in half and the scan ran on past its real end. That is
# not cosmetic - a `//` or `/*` *inside* such a literal was then masked as a
# real comment, so every following byte of live code on the line was deleted
# from the identity and its findings silently re-keyed.

@case('a `//` inside br#".."# is literal text, not a comment')
def _raw_byte_string_comment(e):
    src = 'fn f() { let b = br#"a " b // c"#; let x = 1; }'
    e.equal(masked_as_comment(src, "let x"), False,
            "live code after the literal was masked as a comment")
    _code, comment = masks(src)
    e.equal(norm(src, comment),
            'fn f(){let b=br#"a " b // c"#;let x=1;}',
            "the statement after the literal was deleted from the identity")
    e.equal(labels(src), ["fn f"], "the literal produced a phantom item")


@case('a `/*` and a `{` inside br#".."# open neither a comment nor a block')
def _raw_byte_string_block(e):
    src = ('fn f() {\n'
           '    let s = br#"/* ghost {"#;\n'
           '    let z = 3;\n'
           '}\n')
    e.equal(masked_as_comment(src, "let z"), False,
            "the rest of the function was masked as a block comment")
    e.equal(labels(src), ["fn f"], "the literal produced a phantom item")
    e.equal(scan_items(src)[0]["body_end"], src.rindex("}"),
            "a brace inside the literal was counted")


@case('a trailing backslash in br"C:\\" does not escape the closing quote')
def _raw_byte_string_backslash(e):
    # A raw string has no escapes. Routing it through the normal-string skipper
    # read the backslash as one and ran past the end of the literal.
    src = ('fn f() {\n'
           '    let p = br"C:' + BACKSLASH + '";\n'
           '    let y = 2;\n'
           '}\n')
    e.equal(labels(src), ["fn f"], "the literal swallowed the rest of the file")
    e.equal(scan_items(src)[0]["body_end"], src.rindex("}"),
            "the overrun threw off brace matching")
    _code, comment = masks(src)
    e.equal(norm(src, comment),
            'fn f(){let p=br"C:' + BACKSLASH + '";let y=2;}',
            "code after the literal was lost")


@case('br".." and br#".."# are single literal tokens, emitted verbatim')
def _raw_byte_string_tokens(e):
    e.equal(norm('br"a  b"'), 'br"a  b"', "a raw byte string was reflowed")
    e.equal(norm('br#"a  b"#'), 'br#"a  b"#', "a hashed raw byte string was reflowed")
    e.equal(norm('let s = br#"x, y,"#;'), 'let s=br#"x, y,"#;',
            "comma-dropping reached inside a raw byte string")
    # Two adjacent literals keep a separator; a torn `br` would produce a `b`
    # word token and change this spelling.
    e.equal(norm('br"a" br"b"'), 'br"a" br"b"', "two raw byte strings fused")


@case("`b'x'` is one byte-char literal, not `b` followed by a char")
def _byte_char(e):
    e.equal(norm("b'x'"), "b'x'", "a byte char literal was split")
    e.equal(norm("let n = b'A' + 1;"), "let n=b'A'+1;",
            "`b` was emitted as a separate word token")


@case("an escaped quote inside a char literal closes nothing")
def _escaped_quote_char(e):
    # Hunting for the closing quote from j + 1 lands ON the escaped quote, so
    # `'\''` ended after three bytes, leaving a dangling `'` that then paired
    # with the next quote in the file and hid everything between them.
    src = "let c = '" + BACKSLASH + "''; let s = " + QUOTE + "abc" + QUOTE + ";"
    e.equal(norm(src),
            "let c='" + BACKSLASH + "'';let s=" + QUOTE + "abc" + QUOTE + ";",
            "the escaped-quote literal was split into two tokens")
    arm = "match c { '" + BACKSLASH + "'' => 1, _ => 2 }"
    e.equal(norm(arm), "match c{'" + BACKSLASH + "''=>1,_=>2}",
            "the match-arm form of the escaped-quote literal was split")
    guard = ("fn f() { let c = '" + BACKSLASH + "''; let s = "
             + QUOTE + "}" + QUOTE + "; }")
    e.equal(labels(guard), ["fn f"], "the dangling quote produced a phantom item")
    e.equal(scan_items(guard)[0]["body_end"], guard.rindex("}"),
            "the `}` inside the following string literal was counted as code")


# --------------------------------------------------------------------------
# doc comments belong to the item they document
# --------------------------------------------------------------------------
#
# A `///` block directly above an item is part of that item's header, and the
# doc lints (`missing_errors_doc`, `missing_panics_doc`, `doc_markdown` - 36
# findings in this repository) report spans INSIDE it. While the comment sat
# outside the item, those findings sat outside every item: `item_chain` fell
# through to `<file-scope>` and `enclosing_context` missed its `<hdr>` guard and
# returned a 300-byte window of the FOLLOWING function's signature and body, so
# renaming a local inside that body closed the doc alert and dropped its
# dismissal.

DOC_SIBLINGS = (
    "/// Connect as the offerer.\n"
    "///\n"
    "/// # Errors\n"
    "/// Returns an error when the socket dies.\n"
    "pub async fn connect_offerer(app_id: &str) -> Result<WebRtcState> {\n"
    "    let ws = Ws::new(app_id);\n"
    "    ws.send(Offer)?;\n"
    "    Ok(WebRtcState::new(ws))\n"
    "}\n"
    "\n"
    "/// Connect as the answerer.\n"
    "///\n"
    "/// # Errors\n"
    "/// Returns an error when the socket dies.\n"
    "pub async fn connect_answerer(app_id: &str) -> Result<WebRtcState> {\n"
    "    let ws = Ws::new(app_id);\n"
    "    ws.send(Answer)?;\n"
    "    Ok(WebRtcState::new(ws))\n"
    "}\n"
)

# The same file after a local rename inside both function BODIES. Nothing above
# either `pub async fn` moved, so a doc finding's byte offset is unchanged.
DOC_SIBLINGS_EDITED = DOC_SIBLINGS.replace("ws", "socket_handle")


@case("a doc comment above an item is part of that item's header")
def _doc_header(e):
    e.equal(starts(DOC_SIBLINGS).get("fn connect_offerer"), 0,
            "the header did not extend back over the doc comment")
    e.equal(chain_at(DOC_SIBLINGS, "# Errors"), "fn connect_offerer",
            "a doc finding fell through to file scope")
    e.equal(context_at(DOC_SIBLINGS, "# Errors"), "<hdr>",
            "a doc finding took a window of the function body as its context")


@case("a doc finding's context is invariant to edits in the function body")
def _doc_invariance(e):
    for needle, who in (("Connect as the offerer", "offerer"),
                        ("Connect as the answerer", "answerer")):
        before = context_at(DOC_SIBLINGS, needle)
        after = context_at(DOC_SIBLINGS_EDITED, needle)
        e.equal(after, before, f"renaming a local re-keyed the {who} doc finding")
        e.equal(before, "<hdr>", f"the {who} doc finding is not anchored to a header")
        e.equal(chain_at(DOC_SIBLINGS_EDITED, needle),
                chain_at(DOC_SIBLINGS, needle),
                f"the {who} doc finding's chain moved")


@case("sibling doc findings stay distinct from each other")
def _doc_distinct(e):
    # `<hdr>` is a constant, so the context tier cannot separate two doc
    # findings: the chain has to. (Two findings inside the SAME doc comment are
    # separated by their span instead - see test_identity.py, which measures the
    # whole fingerprint.)
    first = chain_at(DOC_SIBLINGS, "# Errors")
    second = chain_at(DOC_SIBLINGS, "# Errors", DOC_SIBLINGS.index("answerer"))
    e.equal(first, "fn connect_offerer", "first doc block")
    e.equal(second, "fn connect_answerer", "second doc block")
    e.differ(first, second, "the two doc blocks collapsed into one identity")


@case("header attachment stops at a trailing comment, a blank line and `//!`")
def _doc_attachment_limits(e):
    trailing = "fn a() {\n    let x = 1; // why\n}\nfn b() {}\n"
    e.equal(starts(trailing).get("fn b"), trailing.index("fn b"),
            "a trailing `// why` on someone else's statement joined the next item")

    module = "//! module docs\nfn first() {}\n"
    e.equal(starts(module).get("fn first"), module.index("fn first"),
            "an inner doc documents the module, never the item after it")

    blank = "// unrelated note\n\nfn first() {}\n"
    e.equal(starts(blank).get("fn first"), blank.index("fn first"),
            "the walk crossed a blank line")

    attributed = "/// docs\n#[inline]\npub fn g() {}\n"
    e.equal(starts(attributed).get("fn g"), 0,
            "a doc comment interleaved with an attribute was not picked up")
    e.equal(context_at(attributed, "docs"), "<hdr>", "doc above an attribute")


# --------------------------------------------------------------------------
# normalize_tokens
# --------------------------------------------------------------------------

@case("normalize_tokens 1/9: whitespace BETWEEN tokens is not identity")
def _nt_whitespace(e):
    e.equal(norm("foo ( a , b )"), norm("foo(a, b)"), "spacing changed the form")
    e.equal(norm("let  x   =  1 ;"), "let x=1;", "collapsed form")


@case("normalize_tokens 2/9: a rustfmt reflow with a trailing comma is not identity")
def _nt_reflow(e):
    e.equal(norm("foo(\n    a,\n    b,\n)"), norm("foo(a, b)"),
            "rustfmt's trailing comma moved identity")
    e.equal(norm("v.push(1,)"), "v.push(1)", "trailing comma before `)`")
    e.equal(norm("a[1, 2,]"), "a[1,2]", "trailing comma before `]`")
    e.equal(norm("Pair { a, b, }"), "Pair{a,b}", "trailing comma before `}`")
    e.equal(norm("a, b,"), "a,b", "trailing comma at the end of the span")


@case("normalize_tokens 3/9: whitespace INSIDE a string literal IS identity")
def _nt_literal(e):
    e.differ(norm('warn("bad  header")'), norm('warn("bad header")'),
             "two different messages normalized to the same string")
    e.equal(norm('warn("bad  header")'), 'warn("bad  header")',
            "literal content was rewritten")


@case("normalize_tokens 4/9: raw and byte strings are emitted verbatim")
def _nt_raw(e):
    e.equal(norm('r#"raw  text"#'), 'r#"raw  text"#', "raw string was reflowed")
    e.equal(norm('b"by  tes"'), 'b"by  tes"', "byte string was reflowed")
    e.equal(norm('let s = r#"a, b,"#;'), 'let s=r#"a, b,"#;',
            "comma-dropping reached inside a literal")


@case("normalize_tokens 5/9: adjacent words keep a separator, punctuation does not")
def _nt_glue(e):
    e.equal(norm("pub  fn  x"), "pub fn x", "two words fused into one token")
    e.differ(norm("pub fn"), norm("pubfn"), "`pub fn` and `pubfn` are the same code")
    e.equal(norm("a . b ( ) ?"), "a.b()?", "punctuation was padded")
    e.equal(norm('"a"  "b"'), '"a" "b"', "two literals fused")


@case("normalize_tokens 6/9: comments drop with a mask, tokenize without one")
def _nt_comments(e):
    src = "let x = 1; // why  not\nlet y = 2;"
    _code, comment = masks(src)
    e.equal(norm(src, comment), "let x=1;let y=2;", "comment bytes survived the mask")
    e.equal(norm(src, None), "let x=1;//why not let y=2;",
            "without a mask a comment-only span must still normalize to something")
    nested = "let a = 1; /* outer /* inner */ still */ let b = 2;"
    _code2, comment2 = masks(nested)
    e.equal(norm(nested, comment2), "let a=1;let b=2;",
            "a nested block comment leaked into the normalized form")


@case("normalize_tokens 7/9: out-of-range bounds are clamped, not indexed")
def _nt_bounds(e):
    # Clippy reports a byte span; a stale report, or a span that runs to the end
    # of a file whose last line has no newline, can hand this an `hi` past the
    # end. Raising there turns one bad span into a crashed CI step that
    # fingerprints nothing at all.
    e.equal(normalize_tokens("abc", 0, 4, None), "abc", "hi past the end raised")
    e.equal(normalize_tokens("abc", -5, 99, None), "abc", "both bounds out of range")
    e.equal(normalize_tokens("abc", 0, 0, None), "", "an empty span")
    src = "let x = 1; // c\nlet y = 2;"
    _code, comment = masks(src)
    e.equal(normalize_tokens(src, 0, len(src) + 10, comment), "let x=1;let y=2;",
            "hi past the end with a comment mask")


@case("LATENT 1: a one-element tuple normalizes like its parenthesised form")
def _nt_one_element_tuple(e):
    # Pins limitation 1. `(a,)` is a tuple and `(a)` is `a` in parentheses, but
    # trailing commas drop unconditionally, so both normalize to `(a)`. Not
    # fixed: keeping that comma means deciding, for every `(`, whether it opens
    # a call or a tuple, and a `(` judged wrongly turns a rustfmt reflow into a
    # moved fingerprint. Sweeping src/ finds 8 `( X , )` shapes, every one a
    # single-argument call or a reflowed parameter list, and 0 one-element
    # tuples; 4 of the 8 enclose a corpus finding. The rule would judge 8 real
    # constructs to tell apart 0 real ones. This asserts the CURRENT behaviour
    # so it cannot change unnoticed.
    e.equal(norm("(a,)"), "(a)",
            "the documented tuple behaviour changed -- update limitation 1")
    e.equal(norm("(a,)"), norm("(a)"), "a one-element tuple no longer merges")
    e.equal(norm("f((x,))"), norm("f((x))"), "a nested tuple no longer merges")
    e.equal(norm("a, b,"), "a,b", "the end-of-span comma rule changed")


@case("normalize_tokens 9/9: what the unconditional comma drop buys")
def _nt_reflow_invariance(e):
    """Reflow invariance - the property limitation 1 is the price of.

    Every construct here is one rustfmt genuinely reflows, so each must
    normalize identically flat or wrapped. The first three are the ones the
    reverted fix for limitation 1 broke: it classified their `(` as a tuple
    paren, kept the comma, and turned a pure reformat into a moved fingerprint.
    """
    # Callees spelled like item keywords. Both are legal Rust and both occur in
    # real code: the method call `a.union(b)` (HashSet, BTreeSet) and the
    # function-pointer type `fn(T)`.
    e.equal(norm("a.union(\n    b,\n)"), norm("a.union(b)"), "`union` callee reflow")
    e.equal(norm("fn(T,)"), norm("fn(T)"), "function-pointer type")
    e.equal(norm("r#type(\n    x,\n)"), norm("r#type(x)"), "raw-identifier callee reflow")
    # The ordinary shapes.
    e.equal(norm("foo(\n    a,\n    b,\n)"), norm("foo(a, b)"), "multi-argument reflow")
    e.equal(norm("foo(\n    only,\n)"), norm("foo(only)"), "single-argument reflow")
    e.equal(norm("Some(x,)"), "Some(x)", "a one-argument call")
    e.equal(norm("foo::<T>(x,)"), "foo::<T>(x)", "a turbofish call")
    e.equal(norm("(f)(x,)"), "(f)(x)", "calling a parenthesized callee")
    e.equal(norm('println!("x",)'), 'println!("x")', "a macro call")
    e.equal(norm("[a,]"), "[a]", "an array literal")
    e.equal(norm("Pair { a, }"), "Pair{a}", "a struct literal")
    # A span truncated mid-expression has no closing delimiter at all and must
    # still normalize stably rather than raise.
    e.equal(norm("(a,"), norm("(a"), "a truncated span")


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

def run() -> int:
    failed = 0
    for name, probe in CASES:
        expect = Expect()
        try:
            probe(expect)
        except Exception as error:  # a crash is a failure, not a traceback
            expect.failures.append(f"raised {type(error).__name__}: {error}")
        status = "PASS" if not expect.failures else "FAIL"
        print(f"  [{status}] {name}")
        for failure in expect.failures:
            print(f"           {failure}")
        if expect.failures:
            failed += 1
    print()
    print(f"{len(CASES) - failed}/{len(CASES)} scanner cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
