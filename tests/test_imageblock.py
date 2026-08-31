"""Tests for `thrds.imageblock` (`specs/editable-image-blocks.md`)."""
from __future__ import annotations

import pytest

from thrds.imageblock import (
    ImageRef,
    bust_url,
    from_block,
    image_line,
    parse_image_line,
    split_trailing_images,
    strip_bust,
    to_block,
)


# --- parse_image_line ---

def test_parse_plain_image_line():
    assert parse_image_line('![usage card](https://h/card.png)') == ImageRef(
        alt='usage card', url='https://h/card.png',
    )


def test_parse_bust_suffix():
    assert parse_image_line('![card](https://h/card.png){bust}') == ImageRef(
        alt='card', url='https://h/card.png', bust=True,
    )


def test_parse_empty_alt_allowed():
    assert parse_image_line('![](https://h/card.png)') == ImageRef(alt='', url='https://h/card.png')


def test_parse_rejects_custom_emoji_image():
    """`![:name:](name.png)` is mrkdwn's custom-emoji form, not an image block."""
    assert parse_image_line('![:claude:](claude.png)') is None


def test_parse_rejects_trailing_prose():
    assert parse_image_line('![card](https://h/c.png) and more') is None


def test_parse_rejects_leading_prose():
    assert parse_image_line('see ![card](https://h/c.png)') is None


def test_image_line_round_trips():
    for line in (
        '![card](https://h/c.png)',
        '![card](https://h/c.png){bust}',
        '![](https://h/c.png)',
    ):
        ref = parse_image_line(line)
        assert ref is not None
        assert image_line(ref) == line


# --- split_trailing_images ---

def test_split_no_images_returns_content_verbatim():
    assert split_trailing_images('hello\n\nworld') == ('hello\n\nworld', [])


def test_split_single_trailing_image():
    assert split_trailing_images('body\n\n![card](https://h/c.png)') == (
        'body', [ImageRef(alt='card', url='https://h/c.png')],
    )


def test_split_multiple_trailing_images_in_order():
    content = 'body\n\n![a](https://h/a.png)\n\n![b](https://h/b.png){bust}'
    assert split_trailing_images(content) == ('body', [
        ImageRef(alt='a', url='https://h/a.png'),
        ImageRef(alt='b', url='https://h/b.png', bust=True),
    ])


def test_split_mid_message_image_not_lifted():
    content = '![a](https://h/a.png)\n\ntrailing prose'
    assert split_trailing_images(content) == (content, [])


def test_split_run_broken_by_prose_lifts_only_trailing():
    content = '![a](https://h/a.png)\n\nmiddle\n\n![b](https://h/b.png)'
    assert split_trailing_images(content) == (
        '![a](https://h/a.png)\n\nmiddle',
        [ImageRef(alt='b', url='https://h/b.png')],
    )


def test_split_emoji_image_not_lifted():
    content = 'nice one\n\n![:tada:](tada.png)'
    assert split_trailing_images(content) == (content, [])


# --- bust helpers ---

def test_bust_url_no_query():
    assert bust_url('https://h/c.png', '123') == 'https://h/c.png?thrds_bust=123'


def test_bust_url_appends_to_existing_query():
    assert bust_url('https://h/c.png?v=202608', '123') == 'https://h/c.png?v=202608&thrds_bust=123'


def test_bust_url_replaces_prior_token():
    assert bust_url('https://h/c.png?thrds_bust=1', '2') == 'https://h/c.png?thrds_bust=2'


def test_strip_bust_absent_returns_url_byte_identical():
    """No re-encoding pass when there's nothing to strip — caller-versioned
    query strings (`?v=a%2Fb`) survive untouched."""
    assert strip_bust('https://h/c.png?v=a%2Fb') == ('https://h/c.png?v=a%2Fb', False)


def test_strip_bust_present():
    assert strip_bust('https://h/c.png?v=1&thrds_bust=9') == ('https://h/c.png?v=1', True)


def test_strip_bust_only_param():
    assert strip_bust('https://h/c.png?thrds_bust=9') == ('https://h/c.png', True)


# --- to_block / from_block ---

def test_to_block_plain():
    assert to_block(ImageRef(alt='card', url='https://h/c.png')) == {
        'type': 'image', 'image_url': 'https://h/c.png', 'alt_text': 'card',
    }


def test_to_block_bust_appends_token():
    assert to_block(ImageRef(alt='card', url='https://h/c.png', bust=True), token='777') == {
        'type': 'image', 'image_url': 'https://h/c.png?thrds_bust=777', 'alt_text': 'card',
    }


def test_to_block_empty_alt_warns():
    with pytest.warns(UserWarning, match='empty alt text'):
        block = to_block(ImageRef(alt='', url='https://h/c.png'))
    assert block == {'type': 'image', 'image_url': 'https://h/c.png', 'alt_text': ''}


def test_from_block_non_image_is_none():
    assert from_block({'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'hi'}}) is None


def test_block_round_trip_plain():
    ref = ImageRef(alt='card', url='https://h/c.png')
    assert from_block(to_block(ref)) == ref


def test_block_round_trip_with_bust():
    ref = ImageRef(alt='card', url='https://h/c.png?v=1', bust=True)
    assert from_block(to_block(ref, token='777')) == ref
