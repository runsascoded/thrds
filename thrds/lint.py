"""MD-compat linters for platforms that render (or don't render) their own
subset of markdown.

Slack and Discord each render a *different* subset of common markdown; a
document written in one platform's dialect will render badly in the other.
The linters here flag those mismatches in the source `.md` so the author can
fix them before pasting/posting.

Currently:

- :class:`DiscordLinter` — catches the three MD constructs that don't render
  as expected in a normal Discord user message:

  - **masked links** (``[text](url)``) render as literal text in normal user
    messages; only bot-embed messages render them as hyperlinks. The idiomatic
    Discord author uses bare URLs (which auto-linkify).
  - **markdown tables** (``| col | col |``) don't render at all; use a code
    block or bullets.
  - **raw ``@name``** doesn't ping — Discord requires ``<@user_id>`` for a
    real user mention (name-based mentions don't resolve on paste).

Extending to another platform: add a linter class here (each with a
``lint(text: str) -> LintReport`` method); the CLI's platform subgroup wires
its verbs into it. See ``specs/done/discord-platform.md`` for the phase-2
scope this file implements.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LintIssue:
    """One lint finding, anchored to a 1-indexed line + column in the source.

    ``severity`` is either ``"warning"`` or ``"error"`` — today all Discord
    rules are warnings (the doc still renders, just not as the author
    probably intended); reserved for future platforms with hard limits.
    """
    line: int
    column: int
    severity: str  # "warning" | "error"
    rule: str
    message: str


@dataclass
class LintReport:
    """Collection of :class:`LintIssue` from one lint pass.

    Sorted by ``(line, column)`` on construction/append so ``format()``
    output reads top-to-bottom in file order.
    """
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    def append(self, issue: LintIssue) -> None:
        self.issues.append(issue)
        self.issues.sort(key=lambda i: (i.line, i.column))

    def format(self, path: str | None = None) -> str:
        """Human-readable stderr output. ``<path>:<line>:<col>: <severity> [<rule>] <message>``."""
        prefix = f"{path}:" if path else ""
        return "\n".join(
            f"{prefix}{i.line}:{i.column}: {i.severity} [{i.rule}] {i.message}"
            for i in self.issues
        )


# Regex for `[text](url)` — matches non-greedy inside `[]`, then `(...)`. Doesn't
# handle nested brackets (Discord's parser wouldn't either); catches the 99% case.
_MASKED_LINK_RE = re.compile(r"\[[^\]\n]+\]\([^)\n]+\)")

# A table separator line: `|---|`, `|---|---|`, `---|---`, etc. Dashes optionally
# with `:` for alignment. Uses `*` (not `+`) on the extra-column group so
# single-column tables like `|---|` also match.
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")

# A table body line: starts with `|`, has at least one more `|`, no code-fence context.
_TABLE_BODY_RE = re.compile(r"^\s*\|.*\|\s*$")

# `@name` — word chars after `@`, not preceded by another word char (so `x@y` doesn't
# match) and not the well-formed `<@id>` Discord mention. Embedded `.` allowed
# (Discord usernames can contain dots — `alice.smith`) but trailing punctuation
# is excluded via the `(?:\.\w+)*` shape (no bare trailing `.`).
_RAW_MENTION_RE = re.compile(r"(?<![\w<])@([A-Za-z][\w-]*(?:\.\w+)*)")


def _is_fence(line: str) -> bool:
    """A markdown code-fence line (``` or ~~~ optionally followed by a language)."""
    stripped = line.lstrip()
    return stripped.startswith("```") or stripped.startswith("~~~")


class DiscordLinter:
    """Flag markdown constructs that don't render in normal Discord user messages.

    The three rules (masked links, tables, raw ``@name``) are described in the
    module docstring. Code blocks are skipped — anything inside \\`\\`\\` fences
    (or indented code, though we don't detect that) is presumed literal.
    """

    def lint(self, text: str) -> LintReport:
        report = LintReport()
        in_fence = False
        lines = text.split("\n")
        for i, line in enumerate(lines, start=1):
            if _is_fence(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            # Masked links: `[text](url)` renders as literal text in DM/channel messages.
            for m in _MASKED_LINK_RE.finditer(line):
                report.append(LintIssue(
                    line=i,
                    column=m.start() + 1,
                    severity="warning",
                    rule="discord/masked-link",
                    message=f"masked link {m.group()!r} renders as literal text in normal Discord messages; use bare URL",
                ))

            # Tables: separator line + adjacent body lines.
            if _TABLE_SEP_RE.match(line):
                report.append(LintIssue(
                    line=i,
                    column=1,
                    severity="warning",
                    rule="discord/table",
                    message="markdown table doesn't render in Discord; use a code block or bullets",
                ))
            elif _TABLE_BODY_RE.match(line) and self._prev_or_next_is_table_sep(lines, i - 1):
                # Standalone body lines without a separator are likely just prose with pipes;
                # only flag when there's a nearby separator to disambiguate from `foo | bar`.
                report.append(LintIssue(
                    line=i,
                    column=1,
                    severity="warning",
                    rule="discord/table",
                    message="markdown table doesn't render in Discord; use a code block or bullets",
                ))

            # Raw @mentions: `@name` won't ping in Discord (needs `<@id>`).
            for m in _RAW_MENTION_RE.finditer(line):
                report.append(LintIssue(
                    line=i,
                    column=m.start() + 1,
                    severity="warning",
                    rule="discord/raw-mention",
                    message=f"raw @{m.group(1)} won't ping in Discord; use <@user_id> to mention",
                ))

        return report

    @staticmethod
    def _prev_or_next_is_table_sep(lines: list[str], idx: int) -> bool:
        """True iff the line at ``lines[idx-1]`` or ``lines[idx+1]`` matches the
        table-separator regex (idx here is 0-indexed into ``lines``)."""
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(lines) and _TABLE_SEP_RE.match(lines[j]):
                return True
        return False
