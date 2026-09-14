"""Unified image attachments: `Image` + `Msg.images`.

`Msg(content, images=[Image(...)])` carries attachments through `core.sync` to
POST and (when an edit fires) EDIT. A hosted `url` is the cross-platform
currency; only Discord's webhook uploads local bytes. URL-first: platforms that
reference media by URL (Slack) or don't upload (the Discord *bot* API, Bluesky)
raise on an image they can't post. See `specs/unified-image-attachments.md`.
"""
from __future__ import annotations

import pytest

from thrds.core import Image, Msg, _images, _post_kwargs


def test_image_needs_url_or_path():
    with pytest.raises(ValueError, match="needs a `url` or a `path`"):
        Image()


def test_image_bust_needs_url():
    with pytest.raises(ValueError, match="cache-busts a `url`"):
        Image(path="/x/p.png", bust=True)


def test_post_kwargs_carries_images_from_msg():
    im = Image(url="https://x/p.png", alt="plot")
    kw = _post_kwargs(Msg("body", username="A", images=[im]))
    assert kw == {
        "username": "A",
        "icon_url": None,
        "icon_emoji": None,
        "images": [im],
    }


def test_bare_str_entry_has_no_images():
    assert _post_kwargs("body") == {}
    assert _images("body") == ()


def test_images_helper_reads_msg_attachments():
    im = Image(path="/x/p.png")
    assert _images(Msg("body", images=[im])) == [im]
    assert _images(Msg("body")) == ()


def test_slack_post_rejects_path_only_image():
    from thrds.slack import SlackClient

    client = SlackClient("xoxb-tok", "CHAN")
    with pytest.raises(NotImplementedError, match="a hosted `url`"):
        client.post("body", images=[Image(path="/x/p.png")])


def test_slack_edit_rejects_path_only_image():
    from thrds.slack import SlackClient

    client = SlackClient("xoxb-tok", "CHAN")
    with pytest.raises(NotImplementedError, match="a hosted `url`"):
        client.edit("123.45", "body", images=[Image(path="/x/p.png")])


def test_discord_bot_post_rejects_images():
    from thrds.discord import DiscordClient

    client = DiscordClient("bot-tok", "CHAN", "GUILD")
    with pytest.raises(NotImplementedError, match="webhook transport"):
        client.post("body", images=[Image(url="https://x/p.png")])


def test_discord_bot_edit_rejects_images():
    from thrds.discord import DiscordClient

    client = DiscordClient("bot-tok", "CHAN", "GUILD")
    with pytest.raises(NotImplementedError, match="webhook transport"):
        client.edit("m1", "body", images=[Image(url="https://x/p.png")])


def test_bsky_post_rejects_images():
    pytest.importorskip("atproto")  # Bluesky client requires the optional extra
    from thrds.bsky import BskyClient

    client = BskyClient.__new__(BskyClient)  # skip login; the raise precedes any client use
    with pytest.raises(NotImplementedError, match="image embeds aren't wired"):
        client.post("body", images=[Image(url="https://x/p.png")])
