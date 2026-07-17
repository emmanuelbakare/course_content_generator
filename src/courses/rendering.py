"""Safe presentation helpers for author-controlled and generated Markdown."""

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


def render_safe_markdown(content: str):
    """Render Markdown and remove unsafe tags, attributes, and URL protocols."""
    rendered = markdown.markdown(content or '', extensions=['extra', 'sane_lists', 'nl2br'])
    cleaned = bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=['http', 'https', 'mailto'],
        strip=True,
    )
    cleaned = bleach.linkify(cleaned, callbacks=[bleach.callbacks.nofollow])
    return mark_safe(cleaned)
