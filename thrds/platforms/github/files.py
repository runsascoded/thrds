"""Description file read/write operations."""

import re
from functools import partial
from os.path import exists
from pathlib import Path

from utz import proc, err

from .patterns import (
    PR_FILENAME_PATTERN,
    PR_LINK_REF_PATTERN,
    PR_INLINE_LINK_PATTERN,
    H1_TITLE_PATTERN,
    LINK_DEF_PATTERN,
)


def get_expected_description_filename(owner: str = None, repo: str = None, pr_number: str | int = None) -> str:
    """Get the expected description filename based on PR info.

    Returns PR-specific name if info available, otherwise DESCRIPTION.md
    """
    if repo and pr_number:
        return f'{repo}#{pr_number}.md'
    return 'DESCRIPTION.md'


def find_description_file(path: Path = None) -> Path | None:
    """Find the description file (either DESCRIPTION.md or {repo}#{pr}.md)."""
    if path is None:
        path = Path.cwd()

    # First check for PR-specific filename
    for file in path.glob('*#*.md'):
        if PR_FILENAME_PATTERN.match(file.name):
            return file

    # Fallback to DESCRIPTION.md
    desc_file = path / 'DESCRIPTION.md'
    if exists(desc_file):
        return desc_file

    return None


def read_description_from_git(ref: str = 'HEAD', path: Path = None) -> tuple[str | None, Path | None]:
    """Read description file from git at specified ref.

    Returns:
        Tuple of (content, filepath) or (None, None) if not found
    """
    desc_file = find_description_file(path)
    if not desc_file:
        return None, None

    try:
        content = proc.text('git', 'show', f'{ref}:{desc_file.name}', err_ok=True)
        if content:
            # Normalize line endings
            content = content.replace('\r\n', '\n')
            return content, desc_file
        return None, None
    except Exception as e:
        err(f"Error: Could not read {desc_file.name} from {ref}: {e}")
        raise


def write_description_with_link_ref(
    file_path: Path,
    owner: str,
    repo: str,
    pr_number: str | int,
    title: str,
    body: str,
    url: str
) -> None:
    """Write a description file with link-reference style header, properly managing the links footer."""
    pr_ref = f'{owner}/{repo}#{pr_number}'
    link_def = f'[{pr_ref}]: {url}'

    # Remove any placeholder link definitions from body (e.g., [owner/repo#XXXX], [owner/repo#NUMBER])
    if body:
        # Pattern to match placeholder link definitions
        placeholder_pattern = re.compile(
            r'^\[' + re.escape(f'{owner}/{repo}') + r'#(XXXX|XX|[Nn][Uu][Mm][Bb][Ee][Rr])\]:[^\n]*\n?',
            re.MULTILINE
        )
        body = placeholder_pattern.sub('', body)

    # Check if the link def already exists in the body
    pr_link_pattern = re.compile(r'^\[' + re.escape(pr_ref) + r']:', re.MULTILINE)
    link_exists = pr_link_pattern.search(body) if body else False

    # Write the file - preserve exact body content
    with open(file_path, 'w') as f:
        # Write header
        f.write(f'# [{pr_ref}] {title}\n')

        # Write body exactly as GitHub gave it to us
        if body:
            f.write('\n')
            f.write(body)

        # Ensure the link def exists (add it if not)
        if not link_exists:
            # Add blank line if body doesn't end with one
            if body and not body.endswith('\n'):
                f.write('\n')
            # Add blank line before footer section
            if not body or not body.endswith('\n\n'):
                f.write('\n')
            f.write(f'{link_def}\n')
        else:
            # Link exists in body; ensure file ends with newline
            # (GitHub strips trailing newlines from PR descriptions)
            if not body.endswith('\n'):
                f.write('\n')


def read_description_file(path: Path = None, expect_plain: bool = False) -> tuple[str | None, str | None]:
    """Read and parse description file.

    Args:
        path: Directory containing description file (defaults to cwd)
        expect_plain: If True, expect plain "# Title" format (pre-creation).
                     If False, try link-reference formats first (post-creation).

    Returns:
        Tuple of (title, body)
    """
    desc_file = find_description_file(path)
    if not desc_file:
        return None, None

    with open(desc_file, 'r') as f:
        content = f.read()
        lines = content.split('\n')

    if not lines:
        return None, None

    first_line = lines[0].strip()

    if expect_plain:
        # Pre-creation: expect plain "# Title" format only
        match = H1_TITLE_PATTERN.match(first_line)
        if match:
            title = match.group(1).strip()
            body_lines = lines[1:]
            while body_lines and not body_lines[0].strip():
                body_lines = body_lines[1:]
            body = '\n'.join(body_lines).rstrip()
            return title, body

        # If we find a link-reference format when expecting plain, that's an error
        if PR_LINK_REF_PATTERN.match(first_line) or PR_INLINE_LINK_PATTERN.match(first_line):
            err(f"Error: Expected plain format but found link-reference format in {desc_file.name}")
            exit(1)

        return None, None

    # Post-creation: try link-reference formats first
    # Try link-reference style first (preferred)
    match = PR_LINK_REF_PATTERN.match(first_line)
    if match:
        # This is link-reference style, get the title
        title = match.group(2).strip()
        # Strip any placeholder prefix like [owner/repo#XXXX] or [owner/repo#NUMBER]
        title = re.sub(r'^\[?[^/\]]+/[^#\]]+#(XXXX|XX|[Nn][Uu][Mm][Bb][Ee][Rr]|\d+)]?\s*', '', title)
        # Find where the body starts (skip first line and blank lines)
        body_lines = []
        in_body = False
        for line in lines[1:]:
            if in_body or line.strip():
                # Skip link definitions at the end
                if not LINK_DEF_PATTERN.match(line):
                    body_lines.append(line)
                    in_body = True
        # Remove trailing blank lines and link definitions
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        body = '\n'.join(body_lines).rstrip()
        return title, body

    # Try inline link style
    match = PR_INLINE_LINK_PATTERN.match(first_line)
    if match:
        title = match.group(4).strip()
        # Rest is the body (skip the first line and any immediately following blank lines)
        body_lines = lines[1:]
        while body_lines and not body_lines[0].strip():
            body_lines = body_lines[1:]
        body = '\n'.join(body_lines).rstrip()
        return title, body

    # Fallback: first line might just be # Title
    match = H1_TITLE_PATTERN.match(first_line)
    if match:
        title = match.group(1).strip()
        body_lines = lines[1:]
        while body_lines and not body_lines[0].strip():
            body_lines = body_lines[1:]
        body = '\n'.join(body_lines).rstrip()
        return title, body

    return None, None


def upload_image_to_github(
    image_path: str,
    owner: str,
    repo: str,
) -> str | None:
    """Upload an image to GitHub and get the user-attachments URL.

    GitHub stores PR images in a special user-attachments area.
    We use the gh CLI to interact with GitHub's API.
    """
    import base64
    import mimetypes

    if not exists(image_path):
        err(f"Error: Image file not found: {image_path}")
        raise FileNotFoundError(f"Image file not found: {image_path}")

    # Determine MIME type
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type or not mime_type.startswith('image/'):
        err(f"Error: {image_path} doesn't appear to be an image (mime: {mime_type})")
        raise ValueError(f"File is not an image: {mime_type}")

    # Read and encode the image
    with open(image_path, 'rb') as f:
        image_data = f.read()
    encoded = base64.b64encode(image_data).decode('utf-8')

    # Create a data URL
    data_url = f"data:{mime_type};base64,{encoded}"

    # Use gh api to upload via markdown rendering
    # This is a bit of a hack - we render markdown with an image to trigger upload
    markdown_with_image = f'![image]({data_url})'

    try:
        # Use GitHub's markdown API to process the image
        cmd = [
            'gh', 'api',
            '--method', 'POST',
            '/markdown',
            '-f', f'text={markdown_with_image}',
            '-f', 'mode=gfm',
            '-f', f'context={owner}/{repo}'
        ]
        result = proc.text(*cmd, log=None)

        # Extract the uploaded image URL from the rendered HTML
        match = re.search(r'src="(https://github\.com/user-attachments/assets/[^"]+)"', result)
        if match:
            url = match.group(1)
            err(f"Uploaded {image_path} -> {url}")
            return url
        else:
            err(f"Error: Could not extract URL from upload response for {image_path}")
            raise ValueError("Could not extract URL from upload response")
    except Exception as e:
        err(f"Error: Failed to upload {image_path}: {e}")
        raise


def process_images_in_description(body: str, owner: str, repo: str, dry_run: bool = False) -> str:
    """Find local image references and upload them to GitHub."""

    if dry_run:
        # Just find and report what would be uploaded
        pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
        matches = re.findall(pattern, body)
        for alt_text, path in matches:
            if not path.startswith('http'):
                err(f"[DRY-RUN] Would upload image: {path}")
        return body

    def replace_image(match):
        alt_text = match.group(1)
        path = match.group(2)

        # Skip if already a URL
        if path.startswith('http'):
            return match.group(0)

        # Upload the image
        url = upload_image_to_github(path, owner, repo)
        if url:
            # Use <img> tag for consistency with GitHub's format
            return f'<img alt="{alt_text}" src="{url}" />'
        else:
            # Keep original if upload failed
            return match.group(0)

    # Replace markdown image references
    pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    updated_body = re.sub(pattern, replace_image, body)

    return updated_body
