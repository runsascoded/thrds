"""Tests for the Discord MD-compat linter (`thrds.lint.DiscordLinter`)."""
from __future__ import annotations

from thrds.lint import DiscordLinter, LintIssue, LintReport


def _lint(text: str) -> list[LintIssue]:
    return DiscordLinter().lint(text).issues


# --- masked links ---

def test_masked_link_flagged():
    text = "See [the docs](https://example.com) for more.\n"
    issues = _lint(text)
    assert issues == [LintIssue(
        line=1, column=5, severity="warning", rule="discord/masked-link",
        message="masked link '[the docs](https://example.com)' renders as literal text "
                "in normal Discord messages; use bare URL",
    )]


def test_bare_url_not_flagged():
    """Bare URLs auto-linkify in Discord — no warning."""
    text = "See https://example.com for more.\n"
    assert _lint(text) == []


def test_multiple_masked_links_on_one_line():
    text = "[a](https://a.example) and [b](https://b.example)\n"
    issues = _lint(text)
    assert [i.column for i in issues] == [1, 28]
    assert all(i.rule == "discord/masked-link" for i in issues)


def test_masked_link_inside_code_fence_ignored():
    text = "```\n[link](https://x.example)\n```\nnormal [link](https://y.example)\n"
    issues = _lint(text)
    assert len(issues) == 1
    assert issues[0].line == 4  # only the one outside the fence


# --- tables ---

def test_table_flagged_on_separator_line():
    text = "| Col A | Col B |\n|---|---|\n| a | b |\n"
    issues = _lint(text)
    # 3 warnings: separator line + both body lines (they're adjacent to the separator).
    assert [i.line for i in issues] == [1, 2, 3]
    assert all(i.rule == "discord/table" for i in issues)


def test_prose_with_pipes_not_flagged():
    """Line with pipes but no separator anywhere near it is prose, not a table."""
    text = "The cost | benefit tradeoff was clear.\n"
    assert _lint(text) == []


def test_table_inside_code_fence_ignored():
    text = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```\n"
    assert _lint(text) == []


# --- raw @mentions ---

def test_raw_mention_flagged():
    text = "Ping @alice about it.\n"
    issues = _lint(text)
    assert issues == [LintIssue(
        line=1, column=6, severity="warning", rule="discord/raw-mention",
        message="raw @alice won't ping in Discord; use <@user_id> to mention",
    )]


def test_valid_discord_mention_not_flagged():
    """`<@12345>` is Discord's real mention syntax — not a raw @name."""
    text = "Ping <@12345> about it.\n"
    assert _lint(text) == []


def test_email_address_not_flagged():
    """`x@y` (word char before `@`) is presumably an email, not a mention."""
    text = "Contact alice@example.com about it.\n"
    assert _lint(text) == []


def test_raw_mention_inside_code_fence_ignored():
    text = "```\nsee @alice\n```\nsee @bob elsewhere\n"
    issues = _lint(text)
    assert len(issues) == 1
    assert issues[0].line == 4


def test_raw_mention_excludes_trailing_period_punctuation():
    """Trailing sentence-ending punctuation isn't part of the username."""
    text = "Ping @alice.\n"
    issues = _lint(text)
    assert len(issues) == 1
    # The capture group is "alice" — the `.` belongs to the sentence.
    assert "@alice " in issues[0].message or "@alice won't" in issues[0].message
    assert "@alice." not in issues[0].message


def test_raw_mention_includes_embedded_period_dot_username():
    """Discord allows `.` in usernames (`alice.smith`); the dot is part of the name."""
    text = "Ping @alice.smith about it.\n"
    issues = _lint(text)
    assert len(issues) == 1
    assert "@alice.smith " in issues[0].message or "@alice.smith won't" in issues[0].message


# --- report shape ---

def test_report_sorted_by_line_then_column():
    """LintReport keeps issues in (line, column) order across multiple rule types."""
    text = (
        "Line one has @foo and [x](https://y).\n"
        "|---|\n"
    )
    report = DiscordLinter().lint(text)
    assert [(i.line, i.column, i.rule) for i in report.issues] == [
        (1, 14, "discord/raw-mention"),
        (1, 23, "discord/masked-link"),
        (2, 1, "discord/table"),
    ]


def test_report_format_prefixes_path_when_given():
    report = LintReport(issues=[LintIssue(
        line=3, column=5, severity="warning", rule="discord/masked-link",
        message="msg",
    )])
    assert report.format(path="draft.md") == "draft.md:3:5: warning [discord/masked-link] msg"
    assert report.format() == "3:5: warning [discord/masked-link] msg"


def test_report_empty_when_no_issues():
    report = DiscordLinter().lint("just prose, no mentions or links.\n")
    assert report.issues == []
    assert not report.has_issues
    assert not report.has_errors


def test_report_format_empty_string_when_no_issues():
    assert LintReport().format() == ""
