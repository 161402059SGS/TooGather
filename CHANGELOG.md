# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
