# Roadmap

This roadmap shows direction, not promises. Priorities follow what pilot users actually need.

## Now: v0.1 (pilot)

- [x] Five event types with status history
- [x] Upload notes; AI suggests events; people confirm or reject
- [x] AI extraction off by default, switchable per project
- [x] Drift rules: overdue commitments, unlinked changes, risks without owner, stale questions, unreviewed suggestions
- [x] Weekly email digest to project owners
- [x] Roles: admin, owner, member, viewer
- [x] REST API with personal tokens and audit log
- [x] MCP bridge for AI agents
- [x] Docker Compose install, encrypted backup script

## Next: v0.2 (from pilot feedback)

- [ ] Connector framework with a simple interface for community connectors
- [ ] Git connector: summarise commits and flag changes without a linked decision
- [ ] WhatsApp chat export parser (common for project coordination in Indonesia)
- [ ] Word and PDF upload (text extraction)
- [ ] Password reset flow and account deactivation from the web app
- [ ] Per-project AI provider settings, stored encrypted
- [ ] Two-factor authentication (TOTP)
- [ ] Project templates by project type (software, construction, agency, events)

## Later

- [ ] Semantic search (pgvector with a multilingual embedding model)
- [ ] Meeting transcript import from common meeting tools
- [ ] Contradiction detection between confirmed decisions
- [ ] Onboarding brief: "what a new team member needs to know"
- [ ] Optional hosted version for teams that cannot self-host

## Non-goals

Saying no keeps TooGather small enough to maintain. These are out of scope:

- **A chat app or chatbot interface.** Use your existing AI agent through MCP.
- **A task or project management tool.** TooGather remembers decisions and commitments; it does not replace Jira, Trello, or ClickUp.
- **Fully automatic memory.** AI suggestions will always require human confirmation.
- **Multi-agent orchestration, agent hosting, or messaging bots.**
- **Peer-to-peer or offline sync between laptops.** One shared server remains the source of truth.
