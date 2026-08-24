"""Regular expression patterns and parsing utilities for GitHub PR/Issue references."""

import re

# Compiled regex patterns
PR_LINK_REF_PATTERN = re.compile(r'^#\s*\[([^/]+/[^#]+#(?:\d+|XXXX|XX|[Nn][Uu][Mm][Bb][Ee][Rr]))]\s+(.*)$')  # # [org/repo#123] Title or placeholders
PR_INLINE_LINK_PATTERN = re.compile(r'^#\s*\[([^/]+)/([^#]+)#(\d+)](?:\([^)]+\))?\s*(.*)$')  # # [org/repo#123](url) Title
PR_TITLE_PATTERN = re.compile(r'^#\s*\[([^]]+)](?:\([^)]+\))?\s*(.*)$')  # # [owner/repo#num](url) Title
PR_FILENAME_PATTERN = re.compile(r'^([^#]+)#(\d+)\.md$')  # repo#123.md
PR_DIR_PATTERN = re.compile(r'^(?:pr|issue|gh)(\d+)$')  # pr123, issue123, or gh123 (legacy + new)
GH_DIR_PATTERN = re.compile(r'^gh$')  # gh directory
LINK_DEF_PATTERN = re.compile(r'^\[([^]]+)]:\s*https?://')  # [ref]: url (matches at line start)
GIST_ID_PATTERN = re.compile(r'gist\.github\.com[:/]([a-f0-9]{20,32})')  # GitHub gist IDs are typically 20-32 hex chars
GIST_URL_PATTERN = re.compile(r'https://gist\.github\.com/[a-f0-9]+(?:/[a-f0-9]+)?')  # Full gist URL
GIST_URL_WITH_USER_PATTERN = re.compile(r'gist\.github\.com/(?:[^/]+/)?([a-f0-9]+)(?:/([a-f0-9]+))?')  # Gist URL with optional user
GIST_FOOTER_VISIBLE_PATTERN = re.compile(r'\[gist\]\((https://gist\.github\.com/[a-f0-9]+(?:/[a-f0-9]+)?)\)')  # [gist](url) in markdown
GIST_FOOTER_HIDDEN_PATTERN = re.compile(r'<!-- Synced with (https://gist\.github\.com/[a-f0-9]+(?:/[a-f0-9]+)?)')  # HTML comment footer
GITHUB_URL_PATTERN = re.compile(r'github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$')  # GitHub URL pattern
GITHUB_PR_URL_PATTERN = re.compile(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)')  # Full PR URL
GITHUB_ISSUE_URL_PATTERN = re.compile(r'https://github\.com/([^/]+)/([^/]+)/issues/(\d+)')  # Full Issue URL
GITHUB_ITEM_URL_PATTERN = re.compile(r'https://github\.com/([^/]+)/([^/]+)/(pull|issues)/(\d+)')  # Full PR or Issue URL
PR_SPEC_PATTERN = re.compile(r'([^/]+)/([^#]+)#(\d+)')  # owner/repo#number format
H1_TITLE_PATTERN = re.compile(r'^#\s+(.+)$')  # # Title
PR_LINK_IN_H1_PATTERN = re.compile(r'^#\s*\[[^]]+]')  # Check if H1 has [...]


def extract_title_from_first_line(first_line: str) -> str:
    """Extract title from first line of PR description, removing PR reference."""
    title_match = PR_TITLE_PATTERN.match(first_line.strip())
    if title_match:
        return title_match.group(2).strip()
    else:
        # Fallback to just removing the #
        return first_line.strip().lstrip('#').strip()


def parse_pr_spec(pr_spec: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse PR/Issue specification in various formats.

    Args:
        pr_spec: Can be:
            - Full URL: https://github.com/owner/repo/pull/123 or /issues/123
            - Short format: owner/repo#123
            - Just number: 123 (requires being in repo)

    Returns:
        Tuple of (owner, repo, number, type) where type is 'pr' or 'issue'
    """
    # Full PR/Issue URL
    url_match = GITHUB_ITEM_URL_PATTERN.match(pr_spec)
    if url_match:
        owner, repo, item_type, number = url_match.groups()
        return owner, repo, number, 'pr' if item_type == 'pull' else 'issue'

    # owner/repo#number format (assume PR by default, will detect later)
    spec_match = PR_SPEC_PATTERN.match(pr_spec)
    if spec_match:
        return spec_match.groups() + (None,)  # Type will be detected

    # Just a number (assume PR by default, will detect later)
    if pr_spec.isdigit():
        return None, None, pr_spec, None

    return None, None, None, None
