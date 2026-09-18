-- =====================================================================
-- TooGather schema, version 003: connectors, per-project AI settings,
-- and project types.
--
-- What this migration changes, and why:
--
--   1. Project types. A construction project and a software project do
--      not want the same four folders. `projects.template_kind` records
--      which set of folders and which charter the project started from,
--      so the UI can say "this is a construction project" later.
--
--   2. Folder kinds are no longer a fixed list. Each project type names
--      its own folder kinds ("drawings", "site-reports", ...), so the
--      CHECK constraint from 002 is dropped. The meaning of 'custom' is
--      unchanged: a folder a person added, and the only kind that can be
--      deleted.
--
--   3. Per-project AI provider settings. One team may use a local Ollama
--      for a client that forbids cloud AI while another uses OpenAI. The
--      API key is stored encrypted, never in plain text.
--
--   4. Connectors. A connector pulls material into a project on a
--      schedule - commits from a Git repository, and whatever community
--      connectors are added later. Each run writes ordinary `sources`
--      rows, so everything downstream (AI proposals, human review) is
--      exactly the same as for an upload.
--
-- Rule from 001 still applies: never edit an applied migration file.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Project types.
--
-- Free text rather than a CHECK list: adding a project type should be a
-- Python change (toogather/project_types.py), not a migration. Projects
-- that existed before this migration are 'software', because that is what
-- the four original folders describe.
-- ---------------------------------------------------------------------
ALTER TABLE projects ADD COLUMN template_kind TEXT NOT NULL DEFAULT 'software';

-- ---------------------------------------------------------------------
-- 2. Folder kinds become open.
-- ---------------------------------------------------------------------
ALTER TABLE folders DROP CONSTRAINT IF EXISTS folders_kind_check;

-- ---------------------------------------------------------------------
-- 3. Per-project AI provider settings.
--
-- A row exists only when a project overrides the server default, so the
-- absence of a row means "use whatever is in the environment".
--
-- api_key_encrypted holds a Fernet token derived from SECRET_KEY (see
-- toogather/crypto.py). If SECRET_KEY is ever lost, the key cannot be
-- read back; the app treats that as "no key set" and asks for it again
-- rather than failing.
-- ---------------------------------------------------------------------
CREATE TABLE project_ai_settings (
    project_id         UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    base_url           TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL DEFAULT '',
    api_key_encrypted  TEXT NOT NULL DEFAULT '',
    updated_by         UUID REFERENCES users(id),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 4. Connectors.
--
-- `kind` names a connector class registered in toogather/connectors;
-- it is free text so a community connector needs no migration.
--
-- `config` holds that connector's non-secret fields (a repository URL, a
-- branch name). `secret_encrypted` holds its one secret field, encrypted
-- the same way as an AI key. Splitting them means the config can be shown
-- on screen and logged without leaking the token.
--
-- `cursor` is how a connector remembers where it got to - for Git, the
-- last commit it has already imported. It is opaque to everything except
-- the connector that wrote it.
-- ---------------------------------------------------------------------
CREATE TABLE connectors (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id         UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL,
    name               TEXT NOT NULL,
    config             JSONB NOT NULL DEFAULT '{}'::jsonb,
    secret_encrypted   TEXT NOT NULL DEFAULT '',
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    cursor             TEXT NOT NULL DEFAULT '',
    run_every_minutes  INTEGER NOT NULL DEFAULT 60,
    last_run_at        TIMESTAMPTZ,
    last_status        TEXT NOT NULL DEFAULT 'new'
                       CHECK (last_status IN ('new', 'running', 'ok', 'failed')),
    last_note          TEXT NOT NULL DEFAULT '',
    created_by         UUID REFERENCES users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX connectors_project_idx ON connectors (project_id);
-- The worker's "what is due?" query. NULLS FIRST so a connector that has
-- never run is picked up before one that ran an hour ago.
CREATE INDEX connectors_due_idx ON connectors (last_run_at NULLS FIRST) WHERE enabled;

-- ---------------------------------------------------------------------
-- 5. Sources can come from a connector.
--
-- `external_id` is the connector's own identifier for the material (for
-- Git, the newest commit in the batch). The partial unique index makes a
-- repeated run harmless: importing the same commit twice is rejected by
-- the database rather than prevented by careful code.
-- ---------------------------------------------------------------------
ALTER TABLE sources ADD COLUMN connector_id UUID REFERENCES connectors(id) ON DELETE SET NULL;
ALTER TABLE sources ADD COLUMN external_id  TEXT NOT NULL DEFAULT '';

ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_kind_check;
ALTER TABLE sources ADD CONSTRAINT sources_kind_check
    CHECK (kind IN ('upload', 'api', 'connector'));

CREATE UNIQUE INDEX sources_connector_external_idx
    ON sources (connector_id, external_id)
    WHERE connector_id IS NOT NULL AND external_id <> '';
