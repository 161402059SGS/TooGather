-- =====================================================================
-- TooGather schema, version 002: workspace, documents, invites.
--
-- What this migration changes, and why:
--
--   1. Identity without passwords. People now join by typing a display
--      name once. `email` and `password_hash` become optional so a user
--      row can exist without either. Rows created the old way keep
--      working, so an install that already has real accounts is not
--      broken by this migration.
--
--   2. Folders and documents. A project is no longer only a list of
--      events: it holds folders (code context, code documentation,
--      technical, MoM) and Markdown documents inside them.
--
--   3. Invites. A project owner mints an invite link carrying a role.
--      Whoever opens it joins with exactly that role.
--
-- Rule from 001 still applies: never edit an applied migration file.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Passwordless identity.
--
-- A UNIQUE column in PostgreSQL permits many NULLs, so dropping NOT NULL
-- on `email` is enough: name-only users all have email NULL and do not
-- collide with each other.
-- ---------------------------------------------------------------------
ALTER TABLE users ALTER COLUMN email         DROP NOT NULL;
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;

-- ---------------------------------------------------------------------
-- 2. Folders: the drawers inside a project.
--
-- `kind` marks the four seeded folders so the UI can give them their own
-- icon and explanatory text. Anything a person adds later is 'custom'.
-- `position` decides sidebar order; `slug` makes URLs readable.
-- ---------------------------------------------------------------------
CREATE TABLE folders (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'custom'
                CHECK (kind IN ('code_context', 'code_documentation',
                                'technical', 'mom', 'custom')),
    description TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 100,
    created_by  UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, slug)
);
CREATE INDEX folders_project_idx ON folders (project_id, position);

-- ---------------------------------------------------------------------
-- 3. Documents: Markdown, the thing people actually write.
--
-- `doc_kind` separates the three special documents from ordinary notes:
--   charter : "what we are trying to achieve" - one per project
--   skill   : SKILL.md - what an AI agent should read to work on this
--   note    : everything else, lives in a folder
--
-- A charter and a skill doc have folder_id NULL: they belong to the
-- project as a whole, not to a drawer inside it. The two partial unique
-- indexes below keep them to one each per project.
-- ---------------------------------------------------------------------
CREATE TABLE documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    folder_id   UUID REFERENCES folders(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    doc_kind    TEXT NOT NULL DEFAULT 'note'
                CHECK (doc_kind IN ('note', 'charter', 'skill')),
    created_by  UUID REFERENCES users(id),
    updated_by  UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Same 'simple' configuration as events.search: no stemming, so it
    -- behaves identically for Indonesian, English and mixed text.
    search      TSVECTOR GENERATED ALWAYS AS (
                    to_tsvector('simple',
                        coalesce(title, '') || ' ' || coalesce(body, ''))
                ) STORED
);
CREATE INDEX documents_project_idx ON documents (project_id, folder_id);
CREATE INDEX documents_search_idx  ON documents USING GIN (search);

CREATE UNIQUE INDEX documents_one_charter_per_project
    ON documents (project_id) WHERE doc_kind = 'charter';
CREATE UNIQUE INDEX documents_one_skill_per_project
    ON documents (project_id) WHERE doc_kind = 'skill';

-- ---------------------------------------------------------------------
-- 4. Invites: a link that carries a role.
--
-- The code is a long random string, stored in plain text on purpose: an
-- invite is a capability to join one project at one role, not a
-- credential, and an owner must be able to re-read and re-share it.
-- Revoking sets revoked_at; rows are kept so the audit trail survives.
--
-- max_uses NULL means unlimited. `uses` counts accepted joins.
-- ---------------------------------------------------------------------
CREATE TABLE invites (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    code        TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('owner', 'member', 'viewer')),
    label       TEXT NOT NULL DEFAULT '',
    max_uses    INTEGER,
    uses        INTEGER NOT NULL DEFAULT 0,
    created_by  UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX invites_project_idx ON invites (project_id);
