# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Project workspace.** Every project now has folders — Code Context, Code Documentation, Technical, Minutes of Meeting — plus any you add yourself.
- **Markdown documents** inside folders, stored and edited as Markdown.
- **Project charter**, seeded with prompts for what you are trying to achieve, what "done" looks like, and what is out of scope.
- **`SKILL.md` per project**, so an AI agent can read how to work on it. Downloadable at `/projects/<id>/skill.md`.
- **Invite links carrying a role.** Project owners mint a link; whoever opens it joins as owner, member or viewer. Links can be labelled, use-limited, and revoked.
- **Team page** showing who is in a project, at what role, with role changes and removal for owners.

### Changed
- **No more passwords.** A visitor types a display name once and is remembered by a signed session cookie. Project roles attach to that identity and are enforced exactly as before. This removes the login screen, the first-run admin setup, and the login throttle. See "Current limitations" in the README for what this trades away.
- **Redesigned interface**: a sidebar with the project and folder tree, a single content column, and a composer-first home page. Dark mode follows the system setting.
- Anyone can create a project and becomes its owner; project creation is no longer admin-only.
- Project overview now leads with the charter, `SKILL.md` and folders, then the events that need attention.

### Fixed
- The `worker` container reported itself permanently unhealthy. It inherited the web image's HTTP health check but serves no HTTP, so the probe could never pass. The worker now declares no health check.

### Security
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
