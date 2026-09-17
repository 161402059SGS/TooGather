"""
Tests for the workspace helpers and the Markdown renderer.

Both are pure functions over their input, so no database is needed. The
renderer tests are the important ones: they pin down that a document cannot
smuggle script into a page, which is the assumption
`toogather/web/markdown.py` marks its output safe on.
"""

from toogather.web.markdown import first_paragraph, render_markdown
from toogather.workspace import (
    charter_template,
    new_invite_code,
    skill_template,
    slugify,
)

# ---------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------


def test_slugify_lowercases_and_joins_words():
    assert slugify("Code Context") == "code-context"


def test_slugify_strips_accents_rather_than_escaping_them():
    assert slugify("Rencana Anggaran") == "rencana-anggaran"
    assert slugify("Café Notes") == "cafe-notes"


def test_slugify_collapses_punctuation_runs():
    assert slugify("Q3 // 2026 -- plan!!") == "q3-2026-plan"


def test_slugify_falls_back_when_nothing_survives():
    # A name with no Latin characters would otherwise produce an empty slug,
    # which would make an unusable URL and collide with every other such name.
    assert slugify("日本語", fallback="folder") == "folder"
    assert slugify("!!!") == "folder"


def test_slugify_is_length_limited():
    assert len(slugify("word " * 60)) <= 60


# ---------------------------------------------------------------------
# Invite codes
# ---------------------------------------------------------------------


def test_invite_codes_are_unique_and_url_safe():
    codes = {new_invite_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert "/" not in code and "+" not in code and "=" not in code
        assert len(code) >= 20


# ---------------------------------------------------------------------
# Seed templates
# ---------------------------------------------------------------------


def test_charter_names_the_project_and_asks_the_questions():
    body = charter_template("Warehouse System")
    assert "# Warehouse System" in body
    assert "What we are trying to achieve" in body
    assert "out of scope" in body


def test_skill_template_has_frontmatter_with_a_slug_name():
    body = skill_template("Warehouse System")
    assert body.startswith("---\n")
    assert "name: warehouse-system" in body
    assert "Working on Warehouse System" in body
    # It should point an agent at the four folders it will actually find.
    for folder in ("Code Context", "Code Documentation", "Technical", "Minutes of Meeting"):
        assert folder in body


# ---------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------


def test_renders_ordinary_markdown():
    html = str(render_markdown("# Title\n\nSome **bold** text."))
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html


def test_raw_html_is_escaped_not_executed():
    html = str(render_markdown("<script>alert(1)</script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_event_handler_attributes_cannot_get_through():
    html = str(render_markdown('<img src=x onerror="alert(1)">'))
    assert "onerror" not in html or "&lt;img" in html
    assert "<img src=x" not in html


def test_javascript_urls_are_not_linked():
    html = str(render_markdown("[click](javascript:alert(1))"))
    assert 'href="javascript:' not in html


def test_ordinary_links_still_work():
    html = str(render_markdown("[docs](https://example.com/a)"))
    assert 'href="https://example.com/a"' in html


def test_tables_render_because_gfm_is_enabled():
    html = str(render_markdown("| a | b |\n| --- | --- |\n| 1 | 2 |"))
    assert "<table>" in html


def test_empty_input_renders_nothing():
    assert str(render_markdown("")) == ""
    assert str(render_markdown(None)) == ""


# ---------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------


def test_preview_skips_headings_and_returns_the_first_real_line():
    assert first_paragraph("# Title\n\nThe actual sentence.") == "The actual sentence."


def test_preview_skips_list_and_quote_markers():
    assert first_paragraph("# T\n\n- bullet\n\n> quote\n\nReal text.") == "Real text."


def test_preview_truncates_with_an_ellipsis():
    out = first_paragraph("x" * 300, limit=50)
    assert len(out) == 51 and out.endswith("…")


def test_preview_of_nothing_is_empty():
    assert first_paragraph("") == ""
    assert first_paragraph(None) == ""
    assert first_paragraph("# Only a heading") == ""
