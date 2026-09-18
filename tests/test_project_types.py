"""
Tests for the project types themselves.

These are cheap invariants that catch the mistakes a new type actually makes:
a folder slug repeated inside one type (which the database would reject at
seeding time with a unique-violation nobody expects), a kind that no longer
has an icon, or a type that was added to the file but never to ALL_TYPES.
"""

import pytest

from toogather import project_types


@pytest.mark.parametrize("ptype", project_types.ALL_TYPES, ids=lambda t: t.kind)
def test_folder_slugs_are_unique_within_a_type(ptype):
    """`folders` has UNIQUE (project_id, slug), so a repeat would fail seeding."""
    slugs = [f.slug for f in ptype.folders]
    assert len(slugs) == len(set(slugs))


@pytest.mark.parametrize("ptype", project_types.ALL_TYPES, ids=lambda t: t.kind)
def test_every_type_has_folders_and_a_minutes_drawer(ptype):
    assert ptype.folders
    # Every kind of project has meetings, and the MoM folder is what the
    # extractor's output is filed against.
    assert any(f.kind == "mom" for f in ptype.folders)


@pytest.mark.parametrize("ptype", project_types.ALL_TYPES, ids=lambda t: t.kind)
def test_every_folder_kind_has_its_own_icon(ptype):
    for folder in ptype.folders:
        assert project_types.folder_icon(folder.kind) == folder.icon
        assert project_types.folder_icon(folder.kind) != project_types.CUSTOM_FOLDER_ICON


@pytest.mark.parametrize("ptype", project_types.ALL_TYPES, ids=lambda t: t.kind)
def test_every_type_is_findable_by_its_kind(ptype):
    assert project_types.get(ptype.kind) is ptype


def test_type_kinds_are_unique():
    kinds = [t.kind for t in project_types.ALL_TYPES]
    assert len(kinds) == len(set(kinds))


def test_an_unknown_kind_falls_back_to_the_default():
    """
    A project created by a future version, or from a type since removed, must
    still open rather than 500.
    """
    assert project_types.get("aerospace") is project_types.DEFAULT_TYPE
    assert project_types.get(None) is project_types.DEFAULT_TYPE
    assert project_types.get("") is project_types.DEFAULT_TYPE


def test_the_default_is_software_which_is_what_existing_projects_were_migrated_to():
    # Migration 003 defaults template_kind to 'software' for rows that predate
    # it, because the four original folders are the software ones.
    assert project_types.DEFAULT_TYPE.kind == "software"


def test_a_custom_folder_gets_the_generic_mark():
    assert project_types.folder_icon("custom") == project_types.CUSTOM_FOLDER_ICON
