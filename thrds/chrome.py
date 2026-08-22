"""Staging-only chrome: a one-line footer rendered into the message text.

**Why the text and not `blocks`.** Chrome first shipped as `context` blocks
wrapping a `section` body, which kept it structurally unable to leak into a
doc. But Slack removes the *Edit message* affordance from any message carrying
blocks — and a staging channel you can't edit in defeats the point of having
one. Worse, it took the affordance this feature most wanted to enable: pointing
a draft at a different channel by editing the line that says where it's going.

So chrome is the last line of the message text, appended after md→mrkdwn
conversion and stripped before the reverse, so neither direction of the
converter ever sees it. Two things replace the structural guarantee blocks gave:

* :func:`split` strips the footer on pull, so it can't round-trip into a doc.
* `promote_thread` refuses to post a body that still carries one, so a strip
  that somehow failed open fails closed at the only boundary that matters.

**Shape.** One line, ` · `-joined — it sits in the message body at body size,
so it earns its prominence by being short::

    → #oa-amazon-trainium · 01-mfu.md
    → (#marin-alerts) · 02-cw-summary.md
    ✅ #marin-alerts · 02-cw-summary.md

In the second, the arrow itself links to the message being replied to: the ts
is what a machine needs and a human never reads.

The third is a *posted* thread, and it condenses. A draft's chrome carries a
`→` because the target is an affordance — edit it and the draft goes
somewhere else. Once posted that pointer is spent, and the two links it left
behind (`→` target, and the word `posted`) pointed at nearly the same place:
our posted permalink already carries the thread root as `?thread_ts=`, so the
target is derivable from it, and the machine-readable copy lives in
`thrds.yml` regardless. So one link survives, the channel name as its anchor
text, and `✅` replaces the word.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SEP = ' · '
ARROW = '→'
POSTED = '✅'
GIST_HOST = 'https://gist.github.com'

# Slack may echo an emoji back as its shortcode rather than the literal glyph.
# Which one it stores isn't contractual, and guessing wrong would be expensive:
# `_reconcile_chrome` compares rendered footer against live footer as *text*, so
# a spelling flip would read as permanent drift and re-edit every OP on every
# push, forever. Accept both spellings, and normalize before any comparison.
GLYPH_ALIASES = {':white_check_mark:': POSTED}

# `<#C0ABCDEF>` or `<#C0ABCDEF|name>` — Slack's channel mention.
_CHANNEL = r'<#(?P<{name}>[A-Z0-9]+)(?:\|[^>]*)?>'

# A Slack link's channel is its `/archives/<C…>` segment; a *message* link
# adds `/pXXXXXXXXXXYYYYYY`. Both forms are useful to paste: the first aims a
# draft at a channel, the second aims it into a thread.
_PERMALINK_TS = re.compile(r'/archives/(?P<channel>[A-Z0-9]+)(?:/p(?P<ts>\d{6,}))?')
# Copying a link to a message inside a thread yields the reply's ts in the
# path and the *parent's* here. Only the parent can be replied into.
_THREAD_TS_PARAM = re.compile(r'[?&]thread_ts=(?P<ts>\d+\.\d+)')

# Token shapes. `_CHANNEL_RE` is Slack's channel mention, with or without the
# `|name` it sometimes echoes back.
_CHANNEL_RE = re.compile(r'<#(?P<channel>[A-Z0-9]+)(?:\|[^>]*)?>')
_NAME_RE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._-]*')
_PAREN_CHANNEL = re.compile(rf'\({_CHANNEL_RE.pattern}\)')
_LINKED_ARROW = re.compile(rf'<(?P<url>https?://[^>|]+)\|{ARROW}>')
_POSTED_WORD = re.compile(r'<(?P<url>https?://[^>|]+)\|posted>')
# The condensed form: our posted permalink, with `#channel-name` as anchor text.
# Slack won't render a `<#C…>` mention inside link text, so the name has to be
# literal — which is why `render` takes one rather than deriving it from the id.
_POSTED_CHANNEL = re.compile(rf'<(?P<url>https?://[^>|]+)\|#(?P<name>{_NAME_RE.pattern})>')
_GIST = re.compile(rf'<(?P<url>{re.escape(GIST_HOST)}/[^>|]+)\|(?P<filename>[^>]+)>')
# A bare thread filename, for naming a draft written straight into the channel
# before it has a gist file to link to. Only accepted alongside a target: a
# lone `foo.md` line is too ordinary a thing to write in prose to claim.
_BARE_FILE = re.compile(r'(?P<filename>[A-Za-z0-9][A-Za-z0-9._-]*\.md)')


def gist_file_url(gist_id: str, filename: str) -> str:
    """Deep-link to ``filename`` within a gist.

    GitHub anchors each file as ``#file-<name>`` with every non-alphanumeric
    run folded to a single dash, lowercased — so ``01-mfu.md`` is
    ``#file-01-mfu-md``. Without the fragment a six-thread gist opens at the
    top and the reader hunts.
    """
    anchor = re.sub(r'[^a-z0-9]+', '-', filename.lower()).strip('-')
    return f'{GIST_HOST}/{gist_id}#file-{anchor}'


@dataclass(frozen=True)
class ChromeEdit:
    """What a staged message's chrome line says that its local state doesn't.

    Both are applied. Renaming is safe: a commit records a *tree*, so a file
    that changes path is just a different tree, and `git log --follow` /
    `git diff -M` reconstruct the linkage from content. The ``NN`` prefix says
    where a thread sorts and nothing else, so editing it in Slack is a
    perfectly good way to reorder drafts.
    """
    slug: str
    target_was: "object | None" = None
    target_now: "object | None" = None
    renamed_to: str | None = None


@dataclass(frozen=True)
class Chrome:
    """A parsed footer: what it says about where this thread is bound.

    ``channel_name`` is the unresolved ``#name`` form, which appears when Slack
    didn't auto-link what was typed. Callers resolve it to an id; parsing stays
    offline.
    """
    channel: str | None = None
    channel_name: str | None = None
    thread_ts: str | None = None
    filename: str | None = None
    posted_url: str | None = None


def _ts_from_permalink(url: str) -> tuple[str | None, str | None]:
    """``(channel, thread_ts)`` from a Slack link; ``(None, None)`` if not one.

    The ``thread_ts=`` query parameter wins over the path's ``/pXXXX`` when
    both are present. A link copied from a message *inside* a thread carries
    the reply's own ts in the path and its parent's in the query, and only the
    parent is a thread anchor — Slack's tree is two levels deep, so a reply is
    a sibling of every other reply, never a parent.

    Taking the path's ts would be quietly destructive rather than merely
    wrong: ``conversations.replies`` on a reply ts returns *that one message*,
    not the thread, so the reconcile would see a one-message thread and either
    edit that reply into our draft's OP (if it's ours) or post a duplicate
    beside it (if it isn't).

    ``thread_ts`` is None for a channel link, which is exactly the difference
    between "post this at the top of #chan" and "post it into that thread".
    """
    m = _PERMALINK_TS.search(url)
    if m is None:
        return None, None
    parent = _THREAD_TS_PARAM.search(url)
    if parent is not None:
        return m.group('channel'), parent.group('ts')
    digits = m.group('ts')
    ts = f'{digits[:-6]}.{digits[-6:]}' if digits else None
    return m.group('channel'), ts


def render(
    *,
    channel: str | None,
    thread_ts: str | None,
    target_url: str | None,
    posted_url: str | None,
    gist_id: str | None,
    filename: str | None,
    channel_name: str | None = None,
) -> str | None:
    """The footer line, or None when nothing is worth saying.

    ``target_url`` is the permalink of the message being replied to; without
    it a reply target degrades to the plain channel form rather than rendering
    a bare ts nobody reads.

    ``posted_url`` collapses the line to its condensed form (see module
    docstring) — but only alongside a ``channel_name``, since the whole point
    is to name the destination in the one surviving link. Without a name it
    degrades to the three-part draft form rather than rendering a link whose
    anchor text is a channel id, or reaching for the network: this function is
    called once per thread per push and must stay offline.
    """
    if posted_url is not None and channel_name is not None:
        parts = [f'{POSTED} <{posted_url}|#{channel_name}>']
    else:
        parts = []
        if channel is not None:
            if thread_ts is not None and target_url is not None:
                parts.append(f'<{target_url}|{ARROW}> (<#{channel}>)')
            else:
                parts.append(f'{ARROW} <#{channel}>')
        if posted_url is not None:
            parts.append(f'<{posted_url}|posted>')
    if gist_id is not None and filename is not None:
        parts.append(f'<{gist_file_url(gist_id, filename)}|{filename}>')
    return SEP.join(parts) if parts else None


def normalize_glyphs(text: str) -> str:
    """Rewrite shortcode spellings of chrome glyphs to their literal form.

    Applied to what Slack hands back, before it's compared against freshly
    rendered chrome. See :data:`GLYPH_ALIASES` for why.
    """
    for alias, glyph in GLYPH_ALIASES.items():
        text = text.replace(alias, glyph)
    return text


# `<…>` (which may contain spaces, e.g. `<url|link text>`) or a bare run of
# non-space. Tokenizing rather than splitting on ` · ` is what lets a
# hand-written line separate its parts with plain spaces.
_TOKEN_RE = re.compile(r'<[^>]*>|\S+')
_DOT = '·'


def parse(line: str) -> Chrome | None:
    """Parse a chrome line, or None if ``line`` isn't one.

    Deliberately lenient, because this is the one line a human writes by hand
    — Slack's composer can't put a link on the arrow, so the canonical
    ``<url|→>`` form is unauthorable from the UI, and reaching for ``·``
    between parts is nobody's instinct. Parts may be separated by ``·`` or
    just spaces. All of these name the same destination:

    * ``→ <#C0ABCDEF>`` — a mention, what Slack makes of a typed ``#name``
    * ``→ #some-channel`` — the literal text, when it didn't auto-link
    * ``→ https://…/archives/C0ABCDEF`` — a pasted channel link
    * ``→ https://…/archives/C0ABCDEF/p178…`` — a pasted *message* link, which
      aims the draft into that thread
    * ``<https://…|→> (<#C0ABCDEF>)`` — what a push renders it back to
    * ``✅ <https://…|#some-channel>`` — a posted thread's condensed form,
      whose permalink supplies the channel id the ``#name`` only displays

    A trailing ``name.md`` names the thread's file. Anything else in the line
    rejects the whole thing: one stray token means this is prose that happens
    to start with an arrow, and stripping it would eat a line of the draft.
    """
    tokens = _TOKEN_RE.findall(normalize_glyphs(line.strip()))
    if not tokens:
        return None
    chrome = Chrome()
    anchored = False
    i = 0

    def with_(**kw) -> Chrome:
        return Chrome(**{**vars(chrome), **kw})

    if tokens[0] == POSTED and len(tokens) > 1:
        m = _POSTED_CHANNEL.fullmatch(tokens[1])
        if m is None:
            return None
        # The channel id comes free with the permalink, so the name is only
        # ever display text. `thread_ts` deliberately does *not*: this is our
        # own message's permalink, and for a thread we started it would name
        # our OP as the thread to reply into — a fact invented by rendering,
        # not one the target ever asserted. A posted thread is terminal and
        # `pull_chrome_edits` skips it, so there's nothing to infer for.
        chrome, anchored = with_(
            posted_url=m['url'],
            channel=_ts_from_permalink(m['url'])[0],
            channel_name=m['name'],
        ), True
        i = 2
    elif tokens[0] == ARROW and len(tokens) > 1:
        target, i = tokens[1], 2
        m = _CHANNEL_RE.fullmatch(target)
        if m is not None:
            chrome, anchored = with_(channel=m['channel']), True
        elif target.startswith('#') and _NAME_RE.fullmatch(target[1:]):
            chrome, anchored = with_(channel_name=target[1:]), True
        else:
            channel, ts = _ts_from_permalink(target.strip('<>'))
            if channel is None:
                return None
            chrome, anchored = with_(channel=channel, thread_ts=ts), True
    else:
        m = _LINKED_ARROW.fullmatch(tokens[0])
        if m is not None and len(tokens) > 1:
            paren = _PAREN_CHANNEL.fullmatch(tokens[1])
            if paren is None:
                return None
            _, ts = _ts_from_permalink(m['url'])
            chrome, anchored = with_(channel=paren['channel'], thread_ts=ts), True
            i = 2

    for token in tokens[i:]:
        if token == _DOT:
            continue
        m = _POSTED_WORD.fullmatch(token)
        if m is not None:
            chrome = with_(posted_url=m['url'])
            continue
        m = _GIST.fullmatch(token)
        if m is not None:
            # A gist URL is distinctive on its own, which is what lets an
            # untargeted draft still have a chrome line.
            chrome, anchored = with_(filename=m['filename']), True
            continue
        m = _BARE_FILE.fullmatch(token) if anchored else None
        if m is not None:
            chrome = with_(filename=m['filename'])
            continue
        return None
    return chrome if anchored else None


def locate(text: str) -> tuple[int, Chrome] | None:
    """``(line index, chrome)`` for a chrome line, or None if there isn't one.

    Accepts it as the **first or last** line. A push always renders it last,
    but writing a new draft in Slack it's natural to lead with where the thing
    is going, and there's no reason to make that wrong.

    The index is what lets the `richtext` path drop the same line from its
    *rendered markdown*: chrome is authored in Slack mrkdwn (``<#C…>``,
    ``<url|→>``) and re-parsing it after rendering would mean a second grammar.
    Its position is the durable fact, and it's always at an extremity.
    """
    lines = text.rstrip('\n').split('\n')
    if len(lines) < 2:
        return None
    chrome = parse(lines[-1].strip())
    if chrome is not None:
        return len(lines) - 1, chrome
    chrome = parse(lines[0].strip())
    if chrome is not None:
        return 0, chrome
    return None


def drop_line(text: str, index: int) -> str:
    """Remove line ``index`` (0 or last) and the blank space it left behind."""
    lines = text.rstrip('\n').split('\n')
    if index == 0:
        return '\n'.join(lines[1:]).lstrip('\n')
    return '\n'.join(lines[:-1]).rstrip('\n')


def split(text: str) -> tuple[str, Chrome | None]:
    """``(body, chrome)`` — strip a chrome line off raw Slack text.

    Operates on the wire text, before ``to_markdown``, mirroring `render`
    running after ``to_slack``. Text with no chrome comes back untouched, so
    this is safe to call on every message including other people's.
    """
    found = locate(text)
    if found is None:
        return text, None
    index, chrome = found
    return drop_line(text, index), chrome


def has_chrome(text: str) -> bool:
    """Whether ``text`` still carries a footer — the promote-time guard."""
    return split(text)[1] is not None
