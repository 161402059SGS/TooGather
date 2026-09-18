# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-09-18

### Added

#### Workspace and documents
- **Project workspace.** Every project now has folders, plus any you add yourself.
- **Markdown documents** inside folders, stored and edited as Markdown.
- **Project charter**, seeded with prompts for what you are trying to achieve, what "done" looks like, and what is out of scope.
- **`SKILL.md` per project**, so an AI agent can read how to work on it. Downloadable at `/projects/<id>/skill.md`.
- **Invite links carrying a role.** Project owners mint a link; whoever opens it joins as owner, member or viewer. Links can be labelled, use-limited, and revoked.
- **Team page** showing who is in a project, at what role, with role changes and removal for owners.

#### Connectors

- **A connector framework.** A connector brings material into a project on a schedule. It writes ordinary source notes, so everything downstream — AI proposals, human review, drift rules — is unchanged. A connector cannot write confirmed events, which is deliberate: that would be a way to put unreviewed claims into a team's memory. Writing one is a class with a `fetch` method; see the interface and its rules in `toogather/connectors/base.py`.
- **A Git connector.** Keeps a bare, blobless mirror of a repository, checks it on a schedule, and writes new commits up as one note per batch — hash, author, date, subject, body, files touched. With AI extraction on those become proposed changes, and the existing "unlinked change" rule then flags any confirmed change with no decision behind it. At most 100 commits per run, oldest first, so a long backlog arrives in order instead of being skipped.
- **Connectors page** per project, owner-only: set one up, pause it, ask for a check now, see what the last run did.
- `CONNECTORS_ENABLED` and `CONNECTOR_TIMEOUT_SECONDS` to control the whole mechanism for a server.

#### Reading more kinds of file

- **Word and PDF upload.** A `.docx` is unzipped and read from its XML, using only the standard library; a `.pdf` is read page by page. A PDF with no text layer is reported as a scan rather than imported as an empty note.
- **WhatsApp chat exports** are recognised on their own, whether uploaded or pasted, and turned into a clean transcript: wrapped messages joined back together, media placeholders and group notices dropped and counted. Timestamps are copied through exactly as WhatsApp wrote them, because the export does not record its own date format and a guessed deadline is worse than an unparsed one.
- The upload limit is now 8 MB for a file and 2 MB for the text read out of it, and the person is told when a long document was cut.

#### Project types

- **Four project types**: software, construction, agency or consulting, and event. The type is chosen when a project is created and decides which folders it starts with, what the charter asks, and what `SKILL.md` tells an agent. The "where to look" table in `SKILL.md` is generated from the folders that actually exist, so an agent is never sent to a drawer the project does not have.
- Adding a type is a change to `toogather/project_types.py` and nothing else; `template_kind` and `folders.kind` are free text, so no migration is needed.

#### Per-project AI provider

- A project owner can point one project at its own endpoint and model, so one client's contract forbidding cloud AI no longer decides for every project on the server. A project overrides only when both an endpoint and a model are set, so a half-finished override cannot quietly fall back to the server's provider.
- **Secrets are encrypted before they are stored.** Per-project API keys and connector tokens are encrypted with a key derived from `SECRET_KEY` by HKDF, so a database dump or a stolen backup holds ciphertext. What this does not protect against is stated plainly in `toogather/crypto.py`: anyone who can read the server's environment has the key.

#### Your own account

- **An account page.** Change the name you are shown as — it is the same account, so everything you already wrote simply shows the new name — or switch your own account off, which signs you out and keeps your name on your work. Switching off is refused while you are the only owner of a project or the only admin, and says which.

### Changed

#### Joining and the interface
- **No more passwords.** A visitor types a display name once and is remembered by a signed session cookie. Project roles attach to that identity and are enforced exactly as before. This removes the login screen, the first-run admin setup, and the login throttle. See "Current limitations" in the README for what this trades away.
- **Redesigned interface**: a sidebar with the project and folder tree, a single content column, and a composer-first home page. Dark mode follows the system setting.
- Anyone can create a project and becomes its owner; project creation is no longer admin-only.
- Project overview now leads with the charter, `SKILL.md` and folders, then the events that need attention.

#### Elsewhere
- The worker resolves the AI provider per project rather than reading the environment directly.
- Uploads and connectors share one queue in the worker. Uploads go first: somebody is waiting for an upload they just made, and nobody is watching a repository poll.
- The Docker image now installs `git`, and both services mount a shared `toogather-data` volume. That volume already held the generated `SECRET_KEY`; it now also holds the Git connector's mirrors. Mounting the same volume in both services matters — if the generated key differed between them, sessions signed by one would be rejected by the other and stored API keys would stop decrypting.
- `/health` now lists which connectors this build has, so a missing connector can be told from a misconfigured one without reading logs.
- The README's roles table said an admin "acts as owner on every project". That has not been true since roles moved onto projects; it now describes what an admin actually is.

### Fixed

- Every signed-in request raised a `KeyError`. `repo.get_user` filtered on `is_active` but did not select it, and the web app re-checks that column on each request.
- The `worker` container reported itself permanently unhealthy. It inherited the web image's HTTP health check but serves no HTTP, so the probe could never pass. The worker now declares no health check.
- Every unreadable PDF was reported to the uploader as "pypdf is not installed", which was both false and unactionable. The reader imported pypdf's exception base under a name pypdf does not export, so the `ImportError` was caught by the branch meant for a missing package. It now imports only `PdfReader`, so the message cannot be tied to a class name again.
- A Git connector whose first clone died partway - the worker stopped, the disk filled, the network dropped - was wedged permanently. The half-written directory made git refuse that path forever ("destination path already exists and is not an empty directory"), and the only way out was deleting the volume by hand. The remains are now cleared before cloning again, so a failed clone costs one run rather than the connector.
- The sidebar's folder tree vanished on the Review queue, the All events list and any event page. Those three pages render inside a project but did not pass the folders to the template, so the tree emptied exactly as someone navigated into the project.

### Security

- `git` is only ever run with an argument list, never a shell string. Repository URLs are restricted to https, http and ssh: git's `ext::` transport runs an arbitrary command, and a local path would let a project owner read any repository on the server. Branch names are checked against a character allow-list before they reach a command line.
- An access token is put into a repository URL percent-encoded, so a token containing `@` or `/` cannot change which host is contacted, and it is scrubbed out of every message that gets stored or shown.
- Git runs with terminal prompting off and ssh in BatchMode, so a repository asking for credentials fails immediately instead of hanging a worker until the timeout.
- A `.docx` whose XML declares a DTD is refused. `xml.etree.ElementTree` does expand internal entities, so this check is the only thing between a crafted file and a process that grows until it is killed. The whole part is scanned, not just its opening bytes: a DTD must precede the root element, but the comments allowed in front of it can be any length, so a fixed window was trivially stepped over.
- A `.docx` part is read through a capped stream rather than in one go. The size a zip declares for an entry lives in the archive's own header and is therefore attacker-controlled, so a file can claim to be small and still decompress to gigabytes. A 60 MB bomb in a 60 KB file is now refused in a tenth of a second.
- Document Markdown is rendered with raw HTML disabled and link URLs validated, so pasted `<script>` and `javascript:` links are shown as text rather than executed.
- The post-join redirect only accepts same-site paths, so a crafted `?next=` cannot bounce a visitor off-site.

## [0.1.0] - 2026-09-17

### Added
- Project memory with five event types: decision, commitment, change, risk, open question.
- Upload of text notes, transcripts, and chat exports, with AI-suggested events that people confirm or reject.
- AI extraction switch per project, off by default; any OpenAI-compatible endpoint, including local Ollama.
- Drift detection for overdue commitments, unlinked changes, risks without owner, stale questions, and unreviewed suggestions.
- Weekly email digest for project owners.
- Roles (admin, owner, member, viewer), event history, and audit log.
- REST API with personal tokens, and an MCP bridge for AI agents.
- Docker Compose setup, encrypted backup script, and fictional sample data.
