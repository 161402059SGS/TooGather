"""
Turning stored Markdown into HTML that is safe to put on a page.

Documents are written by people on the team, but "people on the team" is not
the same as "trusted to inject script tags" - an invite link is easy to pass
on, and a pasted meeting transcript can contain anything. So the renderer is
configured to treat its input as text, never as HTML.

Two settings do that work:

  html=False      Raw HTML in the source is escaped and shown as text rather
                  than passed through, which is what stops <script> and
                  onerror= attributes from ever reaching the page.

  linkify=False   Bare URLs are not auto-linked. Only an explicit [text](url)
                  becomes a link, and markdown-it validates those against its
                  own deny list, so javascript: and vbscript: URLs are dropped.

Because of this the output needs no separate sanitiser pass.
"""

from __future__ import annotations

from functools import lru_cache

from markdown_it import MarkdownIt
from markupsafe import Markup


@lru_cache(maxsize=1)
def _renderer() -> MarkdownIt:
    """One parser, built once. Tables and strikethrough come from 'gfm-like'."""
    return MarkdownIt("gfm-like", {"html": False, "linkify": False, "typographer": False})


def render_markdown(text: str | None) -> Markup:
    """
    Render Markdown to HTML for a template.

    The result is marked safe, which is correct only because the parser above
    escapes raw HTML. If you ever set html=True, this becomes an XSS hole.
    """
    if not text:
        return Markup("")
    return Markup(_renderer().render(text))


def first_paragraph(text: str | None, limit: int = 160) -> str:
    """
    A one-line plain-text preview for cards and lists.

    Headings, list bullets and blockquote markers are skipped so the preview
    shows the first real sentence rather than the title again.
    """
    if not text:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "-", "*", "|", "`", "_")):
            continue
        return line[:limit] + ("…" if len(line) > limit else "")
    return ""
