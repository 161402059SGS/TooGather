-- =====================================================================
-- TooGather schema, version 004: history for the things people write,
-- and connectors that are pushed to rather than polled.
--
-- What this migration changes, and why:
--
--   1. Documents keep their previous versions. Until now a document was
--      last-write-wins with no trace: two people editing one page meant
--      one of them silently lost their work. Events have always had a
--      history; the pages people actually write in did not.
--
--   2. Events keep their previous text. A status change was already
--      recorded in event_history, but the summary, owner and due date
--      could not be corrected at all - a typo in a decision meant
--      rejecting it and writing a new one, which broke the thread.
--
--   3. A connector can be pushed to instead of polled. The Git and email
--      connectors ask on a schedule; a webhook is called by something
--      else. The worker must not try to "run" one of those, so it is
--      marked here rather than inferred from its kind.
--
-- Rule from 001 still applies: never edit an applied migration file.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Document versions.
--
-- A row is written BEFORE each save and holds what the document looked
-- like until then, so the newest version always lives on `documents`
-- itself and history is purely additive. A document with no rows here
-- has never been edited since it was created.
--
-- BIGSERIAL rather than a UUID: these are written often, never
-- referenced from anywhere else, and are read in insertion order.
-- ---------------------------------------------------------------------
CREATE TABLE document_versions (
    id           BIGSERIAL PRIMARY KEY,
    document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    edited_by    UUID REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX document_versions_doc_idx ON document_versions (document_id, id DESC);

-- ---------------------------------------------------------------------
-- 2. Event revisions.
--
-- The same shape, for the text of an event. Status changes stay in
-- event_history: the two answer different questions ("who agreed this
-- was true?" and "who changed what it says?") and mixing them would
-- make both harder to read.
-- ---------------------------------------------------------------------
CREATE TABLE event_revisions (
    id          BIGSERIAL PRIMARY KEY,
    event_id    UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    summary     TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    owner       TEXT,
    due_date    DATE,
    source_ref  TEXT NOT NULL DEFAULT '',
    edited_by   UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX event_revisions_event_idx ON event_revisions (event_id, id DESC);

-- ---------------------------------------------------------------------
-- 3. Connectors that are pushed to.
--
-- `polls` is FALSE for a connector that waits to be called. The worker's
-- "what is due?" query filters on it, so an inbound connector is never
-- claimed, never marked running, and never reports a failed run for
-- something it was not supposed to do.
--
-- `inbound_code` is the secret in its URL. It is stored in plain text on
-- purpose, exactly like an invite code: it is a capability to post into
-- one project, and an owner has to be able to re-read it to paste it
-- into whatever will be calling it. UNIQUE gives the lookup an index and
-- makes a collision impossible rather than unlikely.
-- ---------------------------------------------------------------------
ALTER TABLE connectors ADD COLUMN polls        BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE connectors ADD COLUMN inbound_code TEXT UNIQUE;

-- The due-connector query now also filters on `polls`, so the partial
-- index that serves it should match.
DROP INDEX IF EXISTS connectors_due_idx;
CREATE INDEX connectors_due_idx ON connectors (last_run_at NULLS FIRST)
    WHERE enabled AND polls;
