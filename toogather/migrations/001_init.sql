-- =====================================================================
-- TooGather schema, version 001.
--
-- Design rules (read before changing anything):
--   1. Every project fact is an "event" with a type. There are only five
--      types: decision, commitment, change, risk, question.
--   2. Events are never deleted by normal use. Status changes are recorded
--      in event_history so the team can always answer "who changed this,
--      and when?".
--   3. Every event should point to where it came from (source_id or
--      source_ref). An answer without a source is a guess.
--   4. Migrations only ever move forward. Never edit an applied file;
--      add 002_..., 003_... instead.
-- =====================================================================

-- gen_random_uuid() is built into PostgreSQL 13+, so no extension is needed.

-- ---------------------------------------------------------------------
-- People who can sign in to the web app.
-- ---------------------------------------------------------------------
CREATE TABLE users (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email          TEXT NOT NULL UNIQUE,
    display_name   TEXT NOT NULL,
    password_hash  TEXT NOT NULL,              -- argon2 hash, never plain text
    is_admin       BOOLEAN NOT NULL DEFAULT FALSE,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- A project is the boundary for data and access.
-- ai_extraction_enabled defaults to FALSE on purpose: some clients do not
-- allow their documents to be sent to an AI provider, so a person must
-- switch it on per project.
-- ---------------------------------------------------------------------
CREATE TABLE projects (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   TEXT NOT NULL,
    description            TEXT NOT NULL DEFAULT '',
    ai_extraction_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    created_by             UUID REFERENCES users(id),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Who can see which project, and what they can do there.
--   owner  : manage members and settings, plus everything a member does
--   member : upload, add events, confirm or reject proposals
--   viewer : read only
-- Admins can see every project without a membership row.
-- ---------------------------------------------------------------------
CREATE TABLE project_members (
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('owner', 'member', 'viewer')),
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);

-- ---------------------------------------------------------------------
-- Raw material that knowledge comes from: meeting notes (MoM),
-- transcripts, chat exports. The worker reads rows with status 'pending'
-- and turns them into proposed events.
-- ---------------------------------------------------------------------
CREATE TABLE sources (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('upload', 'api')),
    filename      TEXT NOT NULL,
    content       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    status_note   TEXT NOT NULL DEFAULT '',     -- human-readable reason for failed/skipped
    uploaded_by   UUID REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);
CREATE INDEX sources_pending_idx ON sources (created_at) WHERE status = 'pending';

-- ---------------------------------------------------------------------
-- The heart of TooGather: one row per project fact.
-- ---------------------------------------------------------------------
CREATE TABLE events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type           TEXT NOT NULL
                   CHECK (type IN ('decision', 'commitment', 'change', 'risk', 'question')),
    summary        TEXT NOT NULL,               -- one line a PM can read at a glance
    detail         TEXT NOT NULL DEFAULT '',
    owner          TEXT,                         -- free text, e.g. "Budi (client IT)"
    status         TEXT NOT NULL DEFAULT 'proposed'
                   CHECK (status IN ('proposed', 'confirmed', 'done', 'superseded', 'rejected')),
    due_date       DATE,                         -- mainly for commitments
    source_id      UUID REFERENCES sources(id) ON DELETE SET NULL,
    source_ref     TEXT NOT NULL DEFAULT '',     -- free text or link: commit hash, ticket, MoM date
    supersedes_id  UUID REFERENCES events(id),   -- this event replaces an older one
    related_id     UUID REFERENCES events(id),   -- e.g. a change that implements a decision
    proposed_by    TEXT NOT NULL CHECK (proposed_by IN ('ai', 'person')),
    created_by     UUID REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Full-text search column. The 'simple' configuration does not stem
    -- words, so it behaves the same for Indonesian, English, and mixed text.
    search         TSVECTOR GENERATED ALWAYS AS (
                       to_tsvector('simple',
                           coalesce(summary, '') || ' ' ||
                           coalesce(detail, '') || ' ' ||
                           coalesce(owner, '') || ' ' ||
                           coalesce(source_ref, ''))
                   ) STORED
);
CREATE INDEX events_project_idx ON events (project_id, type, status);
CREATE INDEX events_search_idx  ON events USING GIN (search);

-- ---------------------------------------------------------------------
-- Every status change, forever. This is the audit trail for events.
-- ---------------------------------------------------------------------
CREATE TABLE event_history (
    id          BIGSERIAL PRIMARY KEY,
    event_id    UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    changed_by  UUID REFERENCES users(id),
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Personal API tokens, used by the MCP bridge and scripts.
-- Only a SHA-256 hash is stored; the plain token is shown once.
-- ---------------------------------------------------------------------
CREATE TABLE api_tokens (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);

-- ---------------------------------------------------------------------
-- What was asked and done through the API (including by AI agents).
-- ---------------------------------------------------------------------
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    token_id    UUID REFERENCES api_tokens(id) ON DELETE SET NULL,
    project_id  UUID REFERENCES projects(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Small key/value store for app-level state, e.g. when the last weekly
-- digest was sent.
-- ---------------------------------------------------------------------
CREATE TABLE app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
