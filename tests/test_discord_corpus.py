"""`DiscordLinter` vs. the shared discord-md fixture corpus.

The corpus (canonical at discord-agent's `fixtures/discord-md/corpus.json`,
vendored here by `scripts/sync-preview-bundle`) is the CommonMark-`spec.json`
of this stack: each case records an input, its expected AST (consumed by the
TS parser's tests), and the lint warnings the input deserves. Neither
implementation is the reference — the corpus is.

The Python linter implements a deliberate *subset* of the corpus's warning
families (it's the cheap pre-push heuristic; the renderer is the authority).
`IMPLEMENTED` declares that subset explicitly so the gap is visible: for
implemented families the linter must agree with the corpus exactly, in both
directions; corpus warnings outside them are asserted to be *known* gaps, not
silent ones.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from thrds.lint import DiscordLinter

CORPUS_PATH = Path(__file__).parent / 'fixtures' / 'discord-md-corpus.json'
CORPUS = json.loads(CORPUS_PATH.read_text())

# Warning families the Python linter implements. `discord/heading-depth` and
# `discord/thematic-break` are renderer-side only: the previewer warns on
# them per-message, but a doc-level lint can't — `---` is thrds' own
# message-separator syntax, and flagging it would warn on every doc (the
# 2026-08-26 table-rule bug, reintroduced on purpose).
IMPLEMENTED = {'discord/table', 'discord/raw-mention'}
KNOWN_UNIMPLEMENTED = {'discord/heading-depth', 'discord/thematic-break'}


def test_corpus_is_present_and_nontrivial():
    # The vendoring script must have snapshotted a real corpus; pin the shape
    # of a case so drift in the schema is caught here, not mid-suite.
    assert len(CORPUS) >= 51
    assert sorted(CORPUS[0]) == ['ast', 'input', 'name', 'verified', 'warnings']


def test_every_corpus_warning_family_is_accounted_for():
    families = {w for case in CORPUS for w in case['warnings']}
    assert families == IMPLEMENTED | KNOWN_UNIMPLEMENTED


@pytest.mark.parametrize('case', CORPUS, ids=[c['name'] for c in CORPUS])
def test_linter_agrees_with_corpus(case: dict):
    got = {i.rule for i in DiscordLinter().lint(case['input']).issues}
    expected = {w for w in case['warnings'] if w in IMPLEMENTED}
    # Exact agreement on the implemented families — no extra warnings (a
    # false positive on corpus input is a linter bug) and none missing.
    assert got == expected
