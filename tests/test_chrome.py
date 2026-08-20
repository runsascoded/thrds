"""Tests for `thrds.chrome` — the staging-only footer line.

Chrome lives in the message *text* rather than in `blocks` because Slack makes
any message carrying blocks uneditable, and editing staged drafts (including
editing the footer itself, to retarget one) is what a staging channel is for.
"""
from __future__ import annotations

import pytest

from thrds.chrome import Chrome, gist_file_url, has_chrome, parse, render, split

PARENT = 'https://openathena.slack.com/archives/C0BN20081CH/p1786980761357209'
POSTED = 'https://openathena.slack.com/archives/C0P/p1786993740250899'


# --- gist_file_url ---


def test_gist_url_anchors_the_file():
    assert gist_file_url('abc123', '01-mfu.md') == (
        'https://gist.github.com/abc123#file-01-mfu-md'
    )


def test_gist_url_folds_runs_of_punctuation():
    assert gist_file_url('abc123', '02-tflops-q.md') == (
        'https://gist.github.com/abc123#file-02-tflops-q-md'
    )


def test_gist_url_lowercases():
    assert gist_file_url('abc123', '03-MFU.MD') == (
        'https://gist.github.com/abc123#file-03-mfu-md'
    )


# --- render ---


def _render(**kw) -> str | None:
    base = dict(channel=None, thread_ts=None, target_url=None,
                posted_url=None, gist_id=None, filename=None)
    return render(**{**base, **kw})


def test_render_top_level_target():
    assert _render(channel='C0T', gist_id='g1', filename='01-mfu.md') == (
        '→ <#C0T> · <https://gist.github.com/g1#file-01-mfu-md|01-mfu.md>'
    )


def test_render_reply_target_links_the_arrow():
    """The ts is what a machine needs and a human never reads, so it's the
    arrow's href rather than visible text."""
    assert _render(channel='C0T', thread_ts='1786980761.357209', target_url=PARENT) == (
        f'<{PARENT}|→> (<#C0T>)'
    )


def test_render_reply_target_degrades_without_a_permalink():
    """Rather than printing a bare ts nobody reads."""
    assert _render(channel='C0T', thread_ts='1786980761.357209') == '→ <#C0T>'


def test_render_all_three_on_one_line():
    assert _render(
        channel='C0T', posted_url=POSTED, gist_id='g1', filename='01-mfu.md',
    ) == (
        f'→ <#C0T> · <{POSTED}|posted> · '
        f'<https://gist.github.com/g1#file-01-mfu-md|01-mfu.md>'
    )


def test_render_gist_only_for_an_untargeted_draft():
    assert _render(gist_id='g1', filename='01-a.md') == (
        '<https://gist.github.com/g1#file-01-a-md|01-a.md>'
    )


def test_render_none_when_nothing_to_say():
    assert _render() is None


def test_render_omits_gist_without_a_filename():
    assert _render(channel='C0T', gist_id='g1') == '→ <#C0T>'


# --- parse ---


def test_parse_top_level():
    assert parse('→ <#C0T>') == Chrome(channel='C0T')


def test_parse_channel_mention_with_a_name():
    """Slack echoes mentions back as `<#C0T|name>` in some payloads."""
    assert parse('→ <#C0T|oa-amazon-trainium>') == Chrome(channel='C0T')


def test_parse_reply_recovers_the_ts_from_the_permalink():
    assert parse(f'<{PARENT}|→> (<#C0BN20081CH>)') == Chrome(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_parse_pasted_permalink():
    """Authoring form: paste a message link after the arrow to aim a draft
    into that thread. Channel and ts both come from the URL."""
    assert parse(f'→ <{PARENT}>') == Chrome(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_parse_pasted_permalink_without_angle_brackets():
    assert parse(f'→ {PARENT}') == Chrome(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_parse_full_footer():
    line = (f'→ <#C0T> · <{POSTED}|posted> · '
            f'<https://gist.github.com/g1#file-01-mfu-md|01-mfu.md>')
    assert parse(line) == Chrome(
        channel='C0T', filename='01-mfu.md', posted_url=POSTED,
    )


def test_parse_gist_only():
    assert parse('<https://gist.github.com/g1#file-01-a-md|01-a.md>') == Chrome(
        filename='01-a.md',
    )


@pytest.mark.parametrize('line', [
    'Just a sentence.',
    'A link: <https://example.com|docs>',
    '→ nowhere in particular',
    '→ <https://example.com/not-slack>',
    '· · ·',
    '',
])
def test_parse_rejects_non_footers(line):
    assert parse(line) is None


def test_parse_rejects_a_footer_with_an_unknown_segment():
    """Every segment must be a known shape — one stray part means the line is
    prose that happens to contain an arrow."""
    assert parse('→ <#C0T> · and then some') is None


def test_parse_rejects_a_target_segment_out_of_position():
    assert parse('<https://gist.github.com/g1#file-a-md|a.md> · → <#C0T>') is None


# --- split ---


def test_split_strips_the_footer():
    body = 'Para one.\n\nPara two.'
    text = f'{body}\n\n→ <#C0T>'
    assert split(text) == (body, Chrome(channel='C0T'))


def test_split_leaves_a_body_with_no_footer_untouched():
    text = 'Para one.\n\nPara two.'
    assert split(text) == (text, None)


def test_split_leaves_a_single_line_body_untouched():
    assert split('→ <#C0T>') == ('→ <#C0T>', None)


def test_split_preserves_interior_blank_lines():
    body = 'One.\n\n\nTwo.'
    assert split(f'{body}\n\n→ <#C0T>')[0] == body


def test_split_accepts_chrome_on_the_first_line():
    """Writing a new draft in Slack it's natural to lead with where it's going;
    a push renders it back to the last line."""
    assert split('→ <#C0T>\n\nActual body.') == ('Actual body.', Chrome(channel='C0T'))


def test_split_prefers_the_last_line_when_both_look_like_chrome():
    assert split('→ <#C0FIRST>\n\nBody.\n\n→ <#C0LAST>') == (
        '→ <#C0FIRST>\n\nBody.', Chrome(channel='C0LAST'),
    )


def test_split_ignores_a_chrome_shaped_line_in_the_middle():
    text = 'Body above.\n\n→ <#C0T>\n\nBody below.'
    assert split(text) == (text, None)


def test_has_chrome_is_the_promote_guard():
    assert has_chrome('Body.\n\n→ <#C0T>') is True
    assert has_chrome('Body.') is False


# --- lenient target forms (what a human can actually type in Slack) ---


def test_parse_bare_channel_name():
    """Slack didn't auto-link it; resolution to an id is the caller's job."""
    assert parse('→ #marin-alerts') == Chrome(channel_name='marin-alerts')


def test_parse_pasted_channel_link_has_no_thread_ts():
    """A channel link means top-level; a message link means into that thread.
    That's the whole difference between the two."""
    assert parse('→ https://openathena.slack.com/archives/C0BQDAK2BRT') == Chrome(
        channel='C0BQDAK2BRT',
    )


def test_parse_names_a_new_thread_by_filename():
    assert parse('→ <#C0T> · 04-idea.md') == Chrome(channel='C0T', filename='04-idea.md')


def test_parse_rejects_a_lone_filename_line():
    """Too ordinary a thing to write in prose to claim as chrome."""
    assert parse('04-idea.md') is None


def test_parse_prefers_the_parent_over_a_replys_own_ts():
    """A link copied from inside a thread carries the reply's ts in the path
    and the parent's in the query. Only the parent is a thread anchor:
    `conversations.replies` on a reply ts returns that one message, not the
    thread, so the reconcile would edit it or duplicate beside it."""
    url = ('https://openathena.slack.com/archives/C0BN20081CH/p1786993740250899'
           '?thread_ts=1786980761.357209&cid=C0BN20081CH')
    assert parse(f'→ {url}') == Chrome(
        channel='C0BN20081CH', thread_ts='1786980761.357209',
    )


def test_parse_uses_the_path_ts_for_a_top_level_message_link():
    """No `thread_ts=` means the link *is* the anchor."""
    url = 'https://openathena.slack.com/archives/C0A/p1786980761357209'
    assert parse(f'→ {url}') == Chrome(channel='C0A', thread_ts='1786980761.357209')


# --- condensed posted form ---

GIST_LINK = 'https://gist.github.com/g1#file-02-cw-summary-md|02-cw-summary.md'


def test_render_condenses_once_posted():
    """One link, channel name as anchor text, `✅` for the word "posted"."""
    assert _render(
        channel='C0P', posted_url=POSTED, channel_name='marin-alerts',
    ) == f'✅ <{POSTED}|#marin-alerts>'


def test_render_condensed_keeps_the_gist_link():
    assert _render(
        channel='C0P', posted_url=POSTED, channel_name='marin-alerts',
        gist_id='g1', filename='02-cw-summary.md',
    ) == f'✅ <{POSTED}|#marin-alerts> · <{GIST_LINK}>'


def test_render_condensed_drops_the_target_arrow_entirely():
    """The permalink carries the thread root, so `→` is spent once posted."""
    assert _render(
        channel='C0P', thread_ts='1786980761.357209', target_url=PARENT,
        posted_url=POSTED, channel_name='marin-alerts',
    ) == f'✅ <{POSTED}|#marin-alerts>'


def test_render_degrades_to_the_long_form_without_a_channel_name():
    """Rather than anchoring the link on a channel id, or reaching for the
    network — `render` is called once per thread per push and stays offline."""
    assert _render(channel='C0P', posted_url=POSTED) == (
        f'→ <#C0P> · <{POSTED}|posted>'
    )


def test_render_drafts_are_untouched_by_the_condensed_form():
    """A name is cached as soon as a thread is posted, so drafts sharing that
    channel would condense too if the glyph keyed off the name alone."""
    assert _render(channel='C0P', channel_name='marin-alerts') == '→ <#C0P>'


def test_parse_condensed_recovers_the_channel_id_from_the_permalink():
    """The `#name` is display text; the id rides along in the URL."""
    assert parse(f'✅ <{POSTED}|#marin-alerts>') == Chrome(
        channel='C0P', channel_name='marin-alerts', posted_url=POSTED,
    )


def test_parse_condensed_with_a_gist_link():
    assert parse(f'✅ <{POSTED}|#marin-alerts> · <{GIST_LINK}>') == Chrome(
        channel='C0P', channel_name='marin-alerts', posted_url=POSTED,
        filename='02-cw-summary.md',
    )


def test_condensed_round_trips_render_to_parse():
    line = _render(
        channel='C0P', posted_url=POSTED, channel_name='marin-alerts',
        gist_id='g1', filename='02-cw-summary.md',
    )
    assert parse(line) == Chrome(
        channel='C0P', channel_name='marin-alerts', posted_url=POSTED,
        filename='02-cw-summary.md',
    )


def test_parse_condensed_never_invents_a_thread_ts():
    """This is *our* message's permalink. For a thread we started, taking its
    path ts would name our own OP as the thread to reply into — a fact
    rendering invented, which the target never asserted."""
    url = 'https://openathena.slack.com/archives/C0P/p1786993740250899'
    assert parse(f'✅ <{url}|#marin-alerts>').thread_ts is None


def test_parse_accepts_the_shortcode_spelling_of_the_glyph():
    """Slack may hand back `:white_check_mark:` for what we sent as `✅`.
    Comparing rendered against live is text equality, so a spelling flip would
    read as permanent drift and re-edit every OP on every push."""
    assert parse(f':white_check_mark: <{POSTED}|#marin-alerts>') == parse(
        f'✅ <{POSTED}|#marin-alerts>'
    )


def test_split_strips_a_condensed_footer():
    """The leak guard. A posted thread's staging copy is still read by
    `pull_threads_staging`, so a footer `parse` rejects would land in the
    pulled markdown as a stray line of content."""
    body = f'The summary.\n\n✅ <{POSTED}|#marin-alerts> · <{GIST_LINK}>'
    assert split(body) == (
        'The summary.',
        Chrome(channel='C0P', channel_name='marin-alerts', posted_url=POSTED,
               filename='02-cw-summary.md'),
    )


def test_has_chrome_catches_the_condensed_form():
    """`promote_thread` refuses a body still carrying a footer."""
    assert has_chrome(f'Body.\n\n✅ <{POSTED}|#marin-alerts>') is True


def test_parse_rejects_a_glyph_on_a_non_channel_link():
    """`✅ <url|see here>` is prose with a checkmark, not chrome."""
    assert parse(f'✅ <{POSTED}|see here>') is None


def test_parse_rejects_a_lone_glyph_line():
    assert parse('✅') is None


def test_parse_rejects_a_glyph_followed_by_prose():
    assert parse('✅ shipped it') is None
