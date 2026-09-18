"""
Setting up a project's workspace: folders, charter, and SKILL.md.

A project is not useful empty. When one is created, TooGather seeds it from a
project type (see toogather/project_types.py) with that type's folders, a
charter ("what are we trying to achieve?") and a SKILL.md that an AI agent can
read to learn how to work on this project.

This module is the renderer; project_types.py is the data. Wording that is the
same for every project type lives here, so a new type only has to supply what
is genuinely different about it.

The seeded text is deliberately written as prompts rather than filler. A
heading with a question under it invites someone to answer; a page of lorem
ipsum invites them to close the tab.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

from toogather import project_types, repo
from toogather.project_types import ProjectType

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


def charter_template(project_name: str, ptype: ProjectType | None = None) -> str:
    """
    The starting charter. Questions, not filler.

    The headings are the same for every project type, because every project
    benefits from answering them. What changes is the prompt underneath, which
    is written in the language of that kind of work.
    """
    ptype = ptype or project_types.DEFAULT_TYPE
    done = "\n".join(f"- [ ] {item}" for item in ptype.done_examples)
    return f"""# {project_name}

## What we are trying to achieve

{ptype.achieve_prompt}

## Why it matters

_What changes for whom once this works?_

## What "done" looks like

{done}

## What is explicitly out of scope

{ptype.scope_prompt}

## Who is involved

| Person | Role | Looks after |
| --- | --- | --- |
| _name_ | _owner / member / viewer_ | _area_ |
"""


def skill_template(project_name: str, ptype: ProjectType | None = None) -> str:
    """
    The starting SKILL.md.

    This is the file an AI agent reads before working on the project. It is
    written for that audience: what the project is, the rules to follow, and
    where to look things up. The "where to look" table is generated from the
    project type's folders, so it always matches the folders that actually
    exist.
    """
    ptype = ptype or project_types.DEFAULT_TYPE
    rules = "\n".join(f"- {rule}" for rule in ptype.agent_rules)
    lookup = "\n".join(
        f"| {folder.read_when} | **{folder.name}** |" for folder in ptype.folders
    )
    return f"""---
name: {slugify(project_name, fallback='project')}
description: >-
  Context an AI agent needs before working on {project_name}. Read this first,
  then read the project charter and the folders listed below.
---

# Working on {project_name}

## What this project is

{ptype.agent_intro}

## Rules to follow

_Things that are true here and are not obvious from the work itself._

{rules}

## Where to look

| If you need | Read |
| --- | --- |
{lookup}

## Decisions that are already made

_Do not relitigate these. If one looks wrong, raise it - do not just change it._

## Open questions

_Where an agent should ask rather than assume._
"""


def seed_project(project_id: str, project_name: str, created_by: str | None,
                 template_kind: str | None = None) -> None:
    """
    Give a brand-new project its folders, charter, and SKILL.md.

    Called once, right after the project row is created. Safe to call on a
    project that already has folders only in the sense that it would add
    duplicates - so do not.
    """
    ptype = project_types.get(template_kind)

    for position, folder in enumerate(ptype.folders, start=1):
        repo.create_folder(
            project_id=project_id,
            name=folder.name,
            slug=folder.slug,
            kind=folder.kind,
            description=folder.description,
            position=position,
            created_by=created_by,
        )

    repo.create_document(
        project_id=project_id,
        folder_id=None,
        title="Project charter",
        body=charter_template(project_name, ptype),
        doc_kind="charter",
        created_by=created_by,
    )
    repo.create_document(
        project_id=project_id,
        folder_id=None,
        title="SKILL.md",
        body=skill_template(project_name, ptype),
        doc_kind="skill",
        created_by=created_by,
    )
