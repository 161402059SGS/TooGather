"""
Tests for the post-join redirect guard.

`safe_next` decides where a visitor lands after typing their name. Its input
comes straight from a query string, so it is the one place an attacker could
turn an invite link into an off-site redirect. These tests pin that shut.

Importing the web app needs the settings it reads at import time; the fixture
below supplies throwaway values so no database is touched.
"""

import os

import pytest

os.environ.setdefault("SECRET_KEY", "test-only-not-a-real-secret-value-xxxxxxxx")
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")

from toogather.web.app import safe_next  # noqa: E402  (must follow the env setup)


@pytest.mark.parametrize(
    "target",
    [
        "//evil.com",                    # protocol-relative: the classic bypass
        "///evil.com",
        "https://evil.com",
        "http://evil.com/path",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "evil.com",                      # no leading slash at all
        "",
        None,
    ],
)
def test_off_site_targets_fall_back_home(target):
    assert safe_next(target) == "/"


@pytest.mark.parametrize(
    "target",
    [
        "/",
        "/projects",
        "/join/abc123",
        "/projects/29aad3a1-67a7-4f1f-af1f-b7ee8b83e647/team",
        "/documents/1?edit=1",
    ],
)
def test_same_site_paths_pass_through_unchanged(target):
    assert safe_next(target) == target
