"""
The Git connector: bring commits into a project as material to review.

What it does, in order:

  1. Keeps a bare mirror of the repository in the connector's own working
     directory, and fetches into it on each run.
  2. Lists the commits on the tracked branch that arrived since the commit it
     recorded last time.
  3. Writes them up as one readable note - hash, author, date, subject, body,
     files touched - and hands that to the project as a source.

From there the ordinary path applies: if the project has AI extraction on,
those commits are proposed as `change` events; a person confirms the ones that
matter. Once confirmed, the existing "unlinked change" drift rule flags any
change that is not tied to a decision, which is the question this connector
exists to raise - *was this agreed anywhere, or did it just happen?*

Why a mirror rather than an API: it works for GitHub, GitLab, Gitea, Bitbucket
and a bare repository on a server in the office, with one code path and no
per-host API client. The cost is disk, which is why the clone is bare and
blobless.

Security notes
--------------

The repository URL is set by a project owner, so it is treated as untrusted:

  * Only https, http and ssh URLs are accepted. Git's `ext::` transport runs
    an arbitrary command, and a local path would let an owner read any
    repository on the server; both are refused by the check below.
  * Git is run with an argument list, never a shell string, and never with a
    URL that could be read as an option.
  * Prompting is disabled - no terminal prompt, and ssh in BatchMode - so a
    repository that wants credentials fails fast instead of hanging a worker
    until the timeout expires.
  * An access token is injected into the URL at call time and scrubbed out of
    every message this module produces, because those messages are stored and
    shown on screen.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from toogather.connectors.base import (
    ConfigField,
    Connector,
    ConnectorError,
    Context,
    FetchResult,
    Item,
)

log = logging.getLogger("toogather.connectors.git")

# One run brings in at most this many commits and leaves the rest for the
# next run, so a repository with years of history does not arrive as one
# unreadable source.
MAX_COMMITS_PER_RUN = 100

# Field and record separators inside `git log` output. Both are control
# characters that cannot occur in a commit message.
_FIELD = "\x1f"
_RECORD = "\x1e"
_LOG_FORMAT = f"format:{_RECORD}%H{_FIELD}%an{_FIELD}%ad{_FIELD}%s{_FIELD}%b{_FIELD}"

_SCP_LIKE = re.compile(r"^[A-Za-z0-9._~%+-]+@[A-Za-z0-9.-]+:[^\s]+$")
_ALLOWED_SCHEMES = ("https", "http", "ssh")

# A branch name goes onto a git command line; git's own rules are stricter
# than this, but this is enough to keep an option or a path out of the slot.
_BRANCH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    body: str
    files: tuple[str, ...]

    @property
    def short(self) -> str:
        return self.sha[:8]


# ---------------------------------------------------------------------
# Pure helpers. These are what the tests exercise; no git needed.
# ---------------------------------------------------------------------


def repo_url_problem(url: str) -> str | None:
    """Why this repository URL cannot be used, or None if it is acceptable."""
    url = url.strip()
    if not url:
        return "A repository URL is required."
    if url.startswith("-"):
        return "A repository URL cannot start with a dash."
    if _SCP_LIKE.match(url):
        return None                               # git@github.com:team/repo.git
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        return (
            "Use an https or ssh repository URL. Local paths and git's ext:: "
            "transport are not accepted, because they would let this server "
            "read repositories it was never given."
        )
    if not parts.netloc:
        return "That URL has no host in it."
    return None


def branch_problem(branch: str) -> str | None:
    """Why this branch name cannot be used, or None. Empty means the default branch."""
    if not branch.strip():
        return None
    if not _BRANCH_OK.match(branch.strip()):
        return "That branch name has characters git does not allow."
    return None


def authenticated_url(url: str, token: str) -> str:
    """
    Put an access token into an https URL.

    Only https is touched: ssh authenticates with a key, and an scp-style URL
    has no place to put a token. The token is percent-encoded so a character
    like `@` or `/` in it cannot change which host is contacted.
    """
    if not token or not url.startswith("https://"):
        return url
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]        # drop any username already there
    # The username is a filler that every major host accepts; the token is the
    # part that authenticates.
    credentials = f"x-access-token:{quote(token, safe='')}"
    return urlunsplit((parts.scheme, f"{credentials}@{host}", parts.path,
                       parts.query, parts.fragment))


def scrub(text: str, token: str) -> str:
    """Remove a token from text that is about to be stored or shown."""
    if not token:
        return text
    return text.replace(token, "***").replace(quote(token, safe=""), "***")


def parse_log(raw: str) -> list[Commit]:
    """
    Turn `git log` output in _LOG_FORMAT into commits.

    Each record is five separator-delimited fields followed by the file names
    that `--name-only` printed. The commit body can contain newlines, which is
    why the file list is separated from it by a field marker rather than by
    trying to guess where the body ended.
    """
    commits: list[Commit] = []
    for chunk in raw.split(_RECORD):
        if not chunk.strip():
            continue
        parts = chunk.split(_FIELD)
        if len(parts) < 5:
            continue                              # not a record we wrote
        sha, author, date, subject, body = parts[:5]
        files_blob = parts[5] if len(parts) > 5 else ""
        files = tuple(line.strip() for line in files_blob.splitlines() if line.strip())
        commits.append(Commit(
            sha=sha.strip(), author=author.strip(), date=date.strip(),
            subject=subject.strip(), body=body.strip(), files=files,
        ))
    return commits


def summarise(commits: list[Commit], repo_url: str, branch: str,
              since: str = "", more_waiting: bool = False) -> str:
    """
    Write the commits up as a note a person - and an extractor - can read.

    Deliberately plain prose and lists. This text is handed to the AI
    extractor as document data, so it states facts and gives no instructions:
    a note that told a model what to do would be indistinguishable from a
    commit message that tried to.
    """
    lines = [
        f"Commits on branch {branch} of {repo_url}.",
        (f"{len(commits)} new commit(s) since {since[:8]}." if since
         else f"The {len(commits)} most recent commit(s); this is the first check "
              f"of this repository."),
    ]
    if more_waiting:
        lines.append(
            "More commits are waiting and will be brought in on the next check."
        )
    lines.append("")

    for commit in commits:
        lines.append(f"## {commit.short} - {commit.subject}")
        lines.append(f"Author: {commit.author}. Date: {commit.date}.")
        if commit.files:
            shown = ", ".join(commit.files[:12])
            extra = f" and {len(commit.files) - 12} more" if len(commit.files) > 12 else ""
            lines.append(f"Files changed: {shown}{extra}.")
        if commit.body:
            lines.append("")
            lines.append(commit.body)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def batch_title(commits: list[Commit], branch: str) -> str:
    """The filename a batch of commits is stored under."""
    if len(commits) == 1:
        return f"Git {branch}: {commits[0].short} {commits[0].subject}"[:200]
    return (f"Git {branch}: {len(commits)} commits "
            f"{commits[0].short}..{commits[-1].short}")[:200]


# ---------------------------------------------------------------------
# The connector.
# ---------------------------------------------------------------------


class GitConnector(Connector):
    kind = "git"
    label = "Git repository"
    description = (
        "Check a repository for new commits and bring them in as material to "
        "review. Commits become proposed changes; confirming one puts it in the "
        "project's memory, where a change with no decision behind it is flagged."
    )
    fields = (
        ConfigField(
            "repo_url", "Repository URL",
            placeholder="https://github.com/team/warehouse.git",
            help="An https or ssh URL. For a private repository over https, add a token below.",
        ),
        ConfigField(
            "branch", "Branch", placeholder="main", required=False,
            help="Leave empty to follow the repository's default branch.",
        ),
        ConfigField(
            "access_token", "Access token", required=False, secret=True,
            help="Only for private https repositories. Stored encrypted; never shown again.",
        ),
    )

    # -- setup -------------------------------------------------------

    def config_problem(self, config: dict) -> str | None:
        problem = super().config_problem(config)
        if problem:
            return problem
        return (repo_url_problem(str(config.get("repo_url", "")))
                or branch_problem(str(config.get("branch", ""))))

    # -- running git -------------------------------------------------

    def _git(self, args: list[str], ctx: Context, token: str = "") -> str:
        """
        Run one git command and return its stdout.

        Every prompt git might raise is turned off: a repository that wants
        credentials must fail immediately, because a worker waiting on an
        invisible password prompt looks exactly like a worker that has hung.
        """
        # PATH and HOME are inherited rather than invented: PATH is how `git`
        # is found at all, and HOME is where an ssh key lives, which is the
        # only way an ssh remote can authenticate. Everything else is set to
        # make git fail fast instead of waiting on a prompt nobody can see.
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(ctx.workdir)),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
            "LC_ALL": "C",
        }
        try:
            done = subprocess.run(                      # noqa: S603 - argument list, no shell
                ["git", *args],
                capture_output=True, text=True, env=env,
                timeout=ctx.timeout_seconds, check=False,
            )
        except FileNotFoundError as exc:
            raise ConnectorError(
                "git is not installed on this server, so the Git connector cannot run."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ConnectorError(
                f"The repository did not respond within {ctx.timeout_seconds} seconds."
            ) from exc

        if done.returncode != 0:
            message = scrub((done.stderr or done.stdout or "").strip(), token)
            raise ConnectorError(f"git failed: {message[:400]}")
        return done.stdout

    def _mirror(self, ctx: Context, url: str, token: str) -> Path:
        """The local bare clone, created on first use and fetched into after that."""
        path = ctx.workdir / f"{ctx.connector_id}.git"
        remote = authenticated_url(url, token)

        if not (path / "HEAD").exists():
            # A clone that died partway - the worker was stopped, the disk
            # filled, the network dropped - leaves a directory with no HEAD in
            # it. git then refuses to clone into that path ever again
            # ("destination path already exists and is not an empty
            # directory"), which would wedge this connector permanently with
            # no way out except deleting the volume by hand. Clear the remains
            # first so a failed clone costs one run, not the connector.
            if path.exists():
                log.info("Clearing an incomplete mirror at %s before cloning again", path)
                shutil.rmtree(path, ignore_errors=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            # --bare: no working tree, nothing is ever checked out.
            # --filter=blob:none: fetch commit and tree metadata, not file
            # contents. We read log output, never files, so the blobs would be
            # pure cost. Servers that do not offer partial clone ignore it and
            # send everything, which still works.
            self._git(["clone", "--bare", "--filter=blob:none", "--quiet", "--", remote,
                       str(path)], ctx, token)
        else:
            # Point the remote at the current URL and token before fetching, so
            # a rotated token takes effect without re-cloning.
            self._git(["-C", str(path), "remote", "set-url", "origin", remote], ctx, token)
            self._git(["-C", str(path), "fetch", "--quiet", "--prune", "origin",
                       "+refs/heads/*:refs/heads/*"], ctx, token)
        return path

    def _branch(self, path: Path, ctx: Context, wanted: str, token: str = "") -> str:
        """The branch to follow: the configured one, or the repository's default."""
        if wanted:
            # rev-parse --quiet exits non-zero and prints nothing when the ref
            # is missing, which _git turns into an error with no message. Give
            # it one that says what to fix.
            try:
                self._git(["-C", str(path), "rev-parse", "--verify", "--quiet",
                           f"refs/heads/{wanted}"], ctx, token)
            except ConnectorError as exc:
                raise ConnectorError(
                    f"The repository has no branch called {wanted}.") from exc
            return wanted
        head = self._git(["-C", str(path), "symbolic-ref", "--short", "--quiet", "HEAD"],
                         ctx, token).strip()
        if not head:
            raise ConnectorError("Could not work out the repository's default branch.")
        return head

    def _cursor_is_usable(self, path: Path, ctx: Context, cursor: str,
                          token: str = "") -> bool:
        """
        Is the recorded commit still in the repository?

        A force-push or a rewritten history can remove it. When that happens
        the connector starts again from the newest commits rather than failing,
        and says so in its note.
        """
        if not cursor:
            return False
        try:
            self._git(["-C", str(path), "cat-file", "-e", f"{cursor}^{{commit}}"], ctx, token)
            return True
        except ConnectorError:
            return False

    # -- the run itself ----------------------------------------------

    def fetch(self, ctx: Context) -> FetchResult:
        url = str(ctx.config.get("repo_url", "")).strip()
        problem = repo_url_problem(url)
        if problem:
            raise ConnectorError(problem)

        token = ctx.secret
        path = self._mirror(ctx, url, token)
        branch = self._branch(path, ctx, str(ctx.config.get("branch", "")).strip(), token)

        cursor = ctx.cursor if self._cursor_is_usable(path, ctx, ctx.cursor, token) else ""
        rewritten = bool(ctx.cursor) and not cursor

        # rev-list first, oldest-first, so a long backlog is imported in order
        # instead of jumping the cursor to the newest commit and losing the
        # middle. It prints hashes only, so it is cheap even on a big range.
        range_args = [f"{cursor}..{branch}"] if cursor else [branch, f"-n{MAX_COMMITS_PER_RUN + 1}"]
        shas = self._git(["-C", str(path), "rev-list", "--no-merges", "--reverse",
                          *range_args], ctx, token).split()
        if not shas:
            return FetchResult(note=f"No new commits on {branch}.")

        more_waiting = len(shas) > MAX_COMMITS_PER_RUN
        batch = shas[:MAX_COMMITS_PER_RUN]
        newest = batch[-1]

        log_range = [f"{cursor}..{newest}"] if cursor else [newest, f"-n{len(batch)}"]
        raw = self._git(["-C", str(path), "log", "--no-merges", "--reverse", "--name-only",
                         "--date=short", f"--pretty={_LOG_FORMAT}", *log_range], ctx, token)
        commits = parse_log(raw)
        if not commits:
            # rev-list found commits but log printed nothing usable. Move the
            # cursor anyway, or every future run will retry the same range.
            return FetchResult(cursor=newest, note="Found commits but could not read them.")

        item = Item(
            external_id=newest,
            title=batch_title(commits, branch),
            content=summarise(commits, url, branch, since=cursor, more_waiting=more_waiting),
        )
        note = f"Brought in {len(commits)} commit(s) from {branch}."
        if rewritten:
            note += " The previous commit was gone from the repository, so this started fresh."
        if more_waiting:
            note += f" {len(shas) - len(batch)} more are waiting for the next check."
        return FetchResult(items=(item,), cursor=newest, note=note)
