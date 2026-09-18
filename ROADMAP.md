# Roadmap

This roadmap shows direction, not promises. Priorities follow what pilot users actually need.

## Shipped: v0.1 (pilot)

- [x] Five event types with status history
- [x] Upload notes; AI suggests events; people confirm or reject
- [x] AI extraction off by default, switchable per project
- [x] Drift rules: overdue commitments, unlinked changes, risks without owner, stale questions, unreviewed suggestions
- [x] Weekly email digest to project owners
- [x] Roles: admin, owner, member, viewer
- [x] REST API with personal tokens and audit log
- [x] MCP bridge for AI agents
- [x] Docker Compose install, encrypted backup script

## Shipped: v0.2

- [x] Project workspace: folders, Markdown documents, a charter, and `SKILL.md` per project
- [x] Passwordless joining and invite links carrying a role
- [x] Connector framework with a simple interface for community connectors
- [x] Git connector: summarise commits and flag changes without a linked decision
- [x] WhatsApp chat export parser (common for project coordination in Indonesia)
- [x] Word and PDF upload (text extraction)
- [x] Per-project AI provider settings, stored encrypted
- [x] Project templates by project type (software, construction, agency, events)
- [x] Account deactivation from the web app

Two items that were on this list were dropped, because the change to passwordless
joining in v0.2 removed the thing they were about:

- **Password reset flow.** There are no passwords to reset. A visitor types a
  display name once and is remembered by a signed session cookie.
- **Two-factor authentication (TOTP).** A second factor authenticates a first
  one, and there is no first factor to bind it to. Adding TOTP now would mean
  inventing an account-recovery story in order to secure something that is not
  yet a credential.

Both come back the day TooGather grows real accounts. Until then the honest
statement is the one in the README: this is a tool for a trusted network, and
authentication belongs in front of it.

## Shipped: v0.3

- [x] Document version history, so two people editing one page stop overwriting each other
- [x] Email mailbox connector (IMAP)
- [x] A plain webhook, so anything that can make an HTTP request can post a note
- [x] Editing an event's text, not only its status
- [x] Bulk confirm and reject in the review queue
- [x] A per-project view of what a connector brought in, separate from what people uploaded

Jira and Linear were on this list and are not built. Both publish outgoing
webhooks, so the webhook connector already covers the common case - point Jira
at the URL and issue transitions arrive as notes. A dedicated connector would
only add reading *from* them on a schedule, which nobody has asked for yet. It
stays on the list below rather than being written speculatively.

## Next: v0.4

- [ ] A Jira or Linear connector that reads on a schedule, if pilot teams want more than their outgoing webhooks give
- [ ] Diffing two document versions, rather than reading them side by side
- [ ] Undoing a bulk review in one step
- [ ] Attachments on an email message, rather than only its text
- [ ] Rate limiting on the webhook endpoint

## Later

- [ ] Semantic search (pgvector with a multilingual embedding model)
- [ ] Meeting transcript import from common meeting tools
- [ ] Contradiction detection between confirmed decisions
- [ ] Onboarding brief: "what a new team member needs to know"
- [ ] Real accounts, and with them password reset and two-factor authentication
- [ ] Optional hosted version for teams that cannot self-host

## Non-goals

Saying no keeps TooGather small enough to maintain. These are out of scope:

- **A chat app or chatbot interface.** Use your existing AI agent through MCP.
- **A task or project management tool.** TooGather remembers decisions and commitments; it does not replace Jira, Trello, or ClickUp.
- **Fully automatic memory.** AI suggestions will always require human confirmation. This applies to connectors too: a connector brings material in, it never writes confirmed events.
- **Multi-agent orchestration, agent hosting, or messaging bots.**
- **Peer-to-peer or offline sync between laptops.** One shared server remains the source of truth.
