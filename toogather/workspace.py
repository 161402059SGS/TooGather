"""
Setting up a project's workspace: folders, charter, and SKILL.md.

A project is not useful empty. When one is created, TooGather seeds it with
four folders, a charter ("what are we trying to achieve?") and a SKILL.md
that an AI agent can read to learn how to work on this project.

The seeded text is deliberately written as prompts rather than filler. A
heading with a question under it invites someone to answer; a page of lorem
ipsum invites them to close the tab.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

from toogather import repo

# Invite codes go in URLs and get pasted into chat, so they use a URL-safe
# alphabet and are long enough that guessing one is not worth trying.
INVITE_CODE_BYTES = 18


def new_invite_code() -> str:
    return secrets.token_urlsafe(INVITE_CODE_BYTES)


def slugify(text: str, fallback: str = "folder") -> str:
    """
    Turn a folder name into something safe for a URL.

    Accented and non-Latin characters are stripped rather than escaped, so
    "Rencana Anggaran" becomes "rencana-anggaran" and a name that is entirely
    non-Latin falls back to a generic slug instead of an empty string.
    """
    normalised = unicodedata.normalize("NFKD", text)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug[:60] or fallback


def unique_slug(project_id: str, name: str) -> str:
    """A slug that is free within this project, adding -2, -3, ... if needed."""
    base = slugify(name)
    slug = base
    suffix = 2
    while repo.slug_is_taken(project_id, slug):
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def charter_template(project_name: str) -> str:
    """The starting charter. Questions, not filler."""
    return f"""# {project_name}

## What we are trying to achieve

_One paragraph. If someone reads only this, what should they understand?_

## Why it matters

_What changes for whom once this works?_

## What "done" looks like

- [ ] _A concrete, checkable outcome_
- [ ] _Another one_

## What is explicitly out of scope

_Naming these now saves an argument later._

## Who is involved

| Person | Role | Looks after |
| --- | --- | --- |
| _name_ | _owner / member / viewer_ | _area_ |
"""


def skill_template(project_name: str) -> str:
    """
    The starting SKILL.md.

    This is the file an AI agent reads before working on the project. It is
    written for that audience: what the project is, the rules to follow, and
    where to look things up.
    """
    return f"""---
name: {slugify(project_name, fallback='project')}
description: >-
  Context an AI agent needs before working on {project_name}. Read this first,
  then read the project charter and the folders listed below.
---

# Working on {project_name}

## What this project is

_Two or three sentences. Enough that an agent stops guessing._

## Rules to follow

_Things that are true here and are not obvious from the code._

- _e.g. "Migrations only move forward: never edit an applied file."_
- _e.g. "All user-facing text is written in plain English, no jargon."_

## Where to look

| If you need | Read |
| --- | --- |
| How the system fits together | **Code Context** |
| How to use or run it | **Code Documentation** |
| Infrastructure and deployment | **Technical** |
| What was decided, and when | **Minutes of Meeting** |

## Decisions that are already made

_Do not relitigate these. If one looks wrong, raise it - do not just change it._

## Open questions

_Where an agent should ask rather than assume._
"""


def seed_project(project_id: str, project_name: str, created_by: str | None) -> None:
    """
    Give a brand-new project its folders, charter, and SKILL.md.

    Called once, right after the project row is created. Safe to call on a
    project that already has folders only in the sense that it would add
    duplicates - so do not.
    """
    for position, folder in enumerate(repo.DEFAULT_FOLDERS, start=1):
        repo.create_folder(
            project_id=project_id,
            name=folder["name"],
            slug=folder["slug"],
            kind=folder["kind"],
            description=folder["description"],
            position=position,
            created_by=created_by,
        )

    repo.create_document(
        project_id=project_id,
        folder_id=None,
        title="Project charter",
        body=charter_template(project_name),
        doc_kind="charter",
        created_by=created_by,
    )
    repo.create_document(
        project_id=project_id,
        folder_id=None,
        title="SKILL.md",
        body=skill_template(project_name),
        doc_kind="skill",
        created_by=created_by,
    )
