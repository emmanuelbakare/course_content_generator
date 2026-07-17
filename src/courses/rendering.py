"""Safe presentation helpers for author-controlled and generated Markdown."""

import re
from html import escape
from uuid import uuid4

import bleach
import markdown
from django.utils.safestring import mark_safe

ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS).union(
    {'p', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'br', 'span', 'div', 'table', 'thead', 'tbody', 'tr', 'th', 'td'}
)
ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'rel'],
    'code': ['class'],
    'pre': ['class'],
    'th': ['align'],
    'td': ['align'],
}
MERMAID_FENCE_PATTERN = re.compile(
    r'^```mermaid[ \t]*\n(?P<source>.*?)(?:\n^```[ \t]*$)', re.MULTILINE | re.DOTALL
)


def render_safe_markdown(content: str):
    """Render Markdown, retaining Mermaid only from explicit fenced code blocks."""
    prepared_content, diagrams = _extract_mermaid_fences(content or '')
    rendered = markdown.markdown(prepared_content, extensions=['extra', 'sane_lists', 'nl2br'])
    cleaned = bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=['http', 'https', 'mailto'],
        strip=True,
    )
    cleaned = bleach.linkify(cleaned, callbacks=[bleach.callbacks.nofollow])
    for placeholder, source in diagrams.items():
        cleaned = cleaned.replace(f'<p>{placeholder}</p>', _mermaid_placeholder(source))
    return mark_safe(cleaned)


def _extract_mermaid_fences(content: str) -> tuple[str, dict[str, str]]:
    """Replace explicit Mermaid fences with random text tokens before sanitization.

    Tokens ensure raw HTML cannot manufacture a Mermaid placeholder. The source is
    escaped only after all ordinary user/LLM HTML has been sanitized.
    """
    diagrams = {}

    def replace(match):
        placeholder = f'COURSE_MERMAID_{uuid4().hex}'
        diagrams[placeholder] = match.group('source').strip()
        return f'\n\n{placeholder}\n\n'

    return MERMAID_FENCE_PATTERN.sub(replace, content), diagrams


def _mermaid_placeholder(source: str) -> str:
    """Build the only trusted diagram markup, with readable no-JS/failure fallback."""
    escaped_source = escape(source, quote=True)
    return (
        f'<div class="mermaid-diagram" data-mermaid-source="{escaped_source}">'
        f'<pre class="mermaid-fallback"><code>{escape(source)}</code></pre>'
        '</div>'
    )
