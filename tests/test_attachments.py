"""Attachments through the declarative model: `Msg.files`.

`Msg(content, files=[...])` carries binary attachments through `core.sync` to
POST and (when an edit fires) EDIT. Only the Discord webhook transport uploads
bytes; platforms that reference hosted media by URL (Slack via a trailing
`![alt](url)` image block) or that can't upload at all (Bluesky, the Discord
*bot* API) **raise** on non-empty `files` rather than silently drop them.
"""
from __future__ import annotations

import pytest

from thrds.core import Msg, _files, _post_kwargs


def test_post_kwargs_carries_files_from_msg():
    kw = _post_kwargs(Msg("body", username="A", files=["/x/p.png"]))
    assert kw == {
        "username": "A",
        "icon_url": None,
        "icon_emoji": None,
        "files": ["/x/p.png"],
    }


def test_bare_str_entry_has_no_files():
    assert _post_kwargs("body") == {}
    assert _files("body") == ()


def test_files_helper_reads_msg_attachments():
    assert _files(Msg("body", files=["/x/p.png"])) == ["/x/p.png"]
    assert _files(Msg("body")) == ()


def test_slack_post_rejects_files():
    from thrds.slack import SlackClient

    client = SlackClient("xoxb-tok", "CHAN")
    with pytest.raises(NotImplementedError, match=r"trailing `!\[alt\]\(url\)`"):
        client.post("body", files=["/x/p.png"])


def test_slack_edit_rejects_files():
    from thrds.slack import SlackClient

    client = SlackClient("xoxb-tok", "CHAN")
    with pytest.raises(NotImplementedError, match=r"trailing `!\[alt\]\(url\)`"):
        client.edit("123.45", "body", files=["/x/p.png"])


def test_discord_bot_post_rejects_files():
    from thrds.discord import DiscordClient

    client = DiscordClient("bot-tok", "CHAN", "GUILD")
    with pytest.raises(NotImplementedError, match="webhook transport"):
        client.post("body", files=["/x/p.png"])


def test_discord_bot_edit_rejects_files():
    from thrds.discord import DiscordClient

    client = DiscordClient("bot-tok", "CHAN", "GUILD")
    with pytest.raises(NotImplementedError, match="webhook transport"):
        client.edit("m1", "body", files=["/x/p.png"])
