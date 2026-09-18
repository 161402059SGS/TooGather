"""
All SQL queries used by TooGather, grouped by subject.

Why one file: when someone needs to know "what touches the events table?",
they can search one place. Every query uses %s parameters, so user input is
always sent separately from the SQL text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime

from toogather import db
from toogather.models import EventStatus, ProposedEvent, Role

# =====================================================================
# Users
# =====================================================================


def count_users() -> int:
    row = db.fetch_one("SELECT count(*) AS n FROM users")
    return int(row["n"]) if row else 0


def create_user(email: str, display_name: str, password_hash: str, is_admin: bool) -> dict:
    return db.fetch_one(
        """
        INSERT INTO users (email, display_name, password_hash, is_admin)
        VALUES (lower(%s), %s, %s, %s)
        RETURNING id, email, display_name, is_admin
        """,
        (email.strip(), display_name.strip(), password_hash, is_admin),
    )


def get_user_by_email(email: str) -> dict | None:
    return db.fetch_one(
        "SELECT * FROM users WHERE email = lower(%s) AND is_active", (email.strip(),)
    )


def get_user(user_id: str) -> dict | None:
    """
    One active person, or None.

    is_active is both filtered on and returned: the web app re-checks it on
    every request, so leaving the column out of the SELECT would make that
    check raise instead of pass.
    """
    return db.fetch_one(
        """
        SELECT id, email, display_name, is_admin, is_active
        FROM users WHERE id = %s AND is_active
        """,
        (user_id,),
    )


def set_display_name(user_id: str, display_name: str) -> None:
    """
    Rename a person.

    Their id does not change, so everything they have already created stays
    attached to them and simply shows the new name.
    """
    db.execute(
        "UPDATE users SET display_name = %s WHERE id = %s", (display_name.strip(), user_id)
    )


def projects_solely_owned_by(user_id: str) -> list[dict]:
    """
    Projects where this person is the only owner.

    Used before someone switches their own account off: leaving a project with
    no owner means nobody can ever manage it again.
    """
    return db.fetch_all(
        """
        SELECT p.id, p.name
        FROM project_members m
        JOIN projects p ON p.id = m.project_id
        WHERE m.user_id = %s AND m.role = 'owner'
          AND (SELECT count(*) FROM project_members o
                WHERE o.project_id = p.id AND o.role = 'owner') = 1
        ORDER BY p.name
        """,
        (user_id,),
    )


def list_users() -> list[dict]:
    """
    Everyone who has ever joined, with how many projects they are on.

    Ordered by name, not email: people who joined by name have no email.
    """
    return db.fetch_all(
        """
        SELECT u.id, u.email, u.display_name, u.is_admin, u.is_active, u.created_at,
               (SELECT count(*) FROM project_members m WHERE m.user_id = u.id) AS project_count
        FROM users u
        ORDER BY u.display_name
        """
    )


def count_admins() -> int:
    row = db.fetch_one("SELECT count(*) AS n FROM users WHERE is_admin AND is_active")
    return int(row["n"]) if row else 0


def set_user_admin(user_id: str, is_admin: bool) -> None:
    db.execute("UPDATE users SET is_admin = %s WHERE id = %s", (is_admin, user_id))


def set_user_active(user_id: str, is_active: bool) -> None:
    """
    Switch a person off without deleting them.

    Their name stays on the events and documents they created; deleting the
    row would blank out that history.
    """
    db.execute("UPDATE users SET is_active = %s WHERE id = %s", (is_active, user_id))


# =====================================================================
# Projects and membership
# =====================================================================


def create_project(name: str, description: str, created_by: str,
                   template_kind: str = "software") -> dict:
    """Create a project and make its creator the owner, in one transaction."""
    with db.transaction() as conn:
        project = conn.execute(
            """
            INSERT INTO projects (name, description, created_by, template_kind)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (name.strip(), description.strip(), created_by, template_kind),
        ).fetchone()
        conn.execute(
            "INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, %s)",
            (project["id"], created_by, Role.OWNER.value),
        )
    return project


def list_projects_for_user(user: dict) -> list[dict]:
    """
    The projects this person belongs to.

    Being an admin does not put a project in this list. An admin runs the
    server; that is not the same as being on every team. They can still reach
    any project through the admin overview, and doing so is recorded.
    """
    return db.fetch_all(
        """
        SELECT p.*, m.role AS my_role
        FROM projects p
        JOIN project_members m ON m.project_id = p.id
        WHERE m.user_id = %s
        ORDER BY p.name
        """,
        (user["id"],),
    )


def list_all_projects() -> list[dict]:
    """
    Every project on the server, for the admin overview.

    Returns counts rather than contents: an admin can see that a project
    exists and who looks after it without reading what is inside it.
    """
    return db.fetch_all(
        """
        SELECT p.id, p.name, p.description, p.created_at,
               (SELECT count(*) FROM project_members m WHERE m.project_id = p.id) AS member_count,
               (SELECT count(*) FROM events e WHERE e.project_id = p.id) AS event_count,
               (SELECT count(*) FROM documents d WHERE d.project_id = p.id) AS document_count,
               (SELECT string_agg(u.display_name, ', ' ORDER BY u.display_name)
                  FROM project_members m JOIN users u ON u.id = m.user_id
                 WHERE m.project_id = p.id AND m.role = 'owner') AS owners
        FROM projects p
        ORDER BY p.name
        """
    )


def get_project(project_id: str) -> dict | None:
    return db.fetch_one("SELECT * FROM projects WHERE id = %s", (project_id,))


def get_role(project_id: str, user: dict) -> str | None:
    """
    Return the user's role in a project, or None if they have no access.

    An admin gets no special role here. Whoever runs the server can read the
    database regardless, so pretending otherwise would be theatre; what this
    buys is that an admin's access is deliberate and leaves a trace in the
    audit log, instead of every project silently appearing in their sidebar.
    """
    row = db.fetch_one(
        "SELECT role FROM project_members WHERE project_id = %s AND user_id = %s",
        (project_id, user["id"]),
    )
    return row["role"] if row else None


def set_ai_extraction(project_id: str, enabled: bool) -> None:
    db.execute(
        "UPDATE projects SET ai_extraction_enabled = %s WHERE id = %s", (enabled, project_id)
    )


def list_members(project_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT u.id, u.email, u.display_name, m.role, m.added_at
        FROM project_members m
        JOIN users u ON u.id = m.user_id
        WHERE m.project_id = %s
        ORDER BY u.display_name
        """,
        (project_id,),
    )


def upsert_member(project_id: str, user_id: str, role: str) -> None:
    db.execute(
        """
        INSERT INTO project_members (project_id, user_id, role)
        VALUES (%s, %s, %s)
        ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role
        """,
        (project_id, user_id, role),
    )


def remove_member(project_id: str, user_id: str) -> None:
    db.execute(
        "DELETE FROM project_members WHERE project_id = %s AND user_id = %s",
        (project_id, user_id),
    )


def count_owners(project_id: str) -> int:
    row = db.fetch_one(
        "SELECT count(*) AS n FROM project_members WHERE project_id = %s AND role = 'owner'",
        (project_id,),
    )
    return int(row["n"]) if row else 0


# =====================================================================
# Sources (uploaded raw material)
# =====================================================================


def create_source(project_id: str, filename: str, content: str, user_id: str, kind: str) -> dict:
    return db.fetch_one(
        """
        INSERT INTO sources (project_id, kind, filename, content, uploaded_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, filename, status, created_at
        """,
        (project_id, kind, filename, content, user_id),
    )


def create_connector_source(project_id: str, connector_id: str, external_id: str,
                            filename: str, content: str) -> dict | None:
    """
    Store material a connector brought in, or None if it is already here.

    The partial unique index on (connector_id, external_id) does the
    de-duplication, so a connector that re-reads the same commit after a crash
    cannot create a second copy. ON CONFLICT DO NOTHING turns that into a
    quiet skip rather than an error the worker has to catch.
    """
    return db.fetch_one(
        """
        INSERT INTO sources (project_id, kind, filename, content, connector_id, external_id)
        VALUES (%s, 'connector', %s, %s, %s, %s)
        ON CONFLICT (connector_id, external_id)
            WHERE connector_id IS NOT NULL AND external_id <> ''
            DO NOTHING
        RETURNING id, filename, status, created_at
        """,
        (project_id, filename, content, connector_id, external_id),
    )


def list_sources(project_id: str, limit: int = 20, kind: str = "") -> list[dict]:
    """
    What has been brought into this project, newest first.

    `kind` narrows it to one origin - 'upload', 'api' or 'connector' - for the
    page that answers "which of this did a person put here, and which arrived
    on its own?". An unknown kind is ignored rather than returning nothing,
    because it can only come from a query string.
    """
    conditions = ["s.project_id = %(project_id)s"]
    params: dict = {"project_id": project_id, "limit": limit}
    if kind in ("upload", "api", "connector"):
        conditions.append("s.kind = %(kind)s")
        params["kind"] = kind

    # The WHERE clause is built only from fixed strings above; user values are
    # always passed as parameters.
    sql = f"""
        SELECT s.id, s.filename, s.status, s.status_note, s.created_at, s.processed_at,
               s.kind, s.external_id, u.display_name AS uploaded_by_name,
               c.name AS connector_name, c.kind AS connector_kind,
               (SELECT count(*) FROM events e WHERE e.source_id = s.id) AS event_count
        FROM sources s
        LEFT JOIN users u ON u.id = s.uploaded_by
        LEFT JOIN connectors c ON c.id = s.connector_id
        WHERE {" AND ".join(conditions)}
        ORDER BY s.created_at DESC
        LIMIT %(limit)s
    """
    return db.fetch_all(sql, params)


def count_sources_by_kind(project_id: str) -> dict[str, int]:
    rows = db.fetch_all(
        "SELECT kind, count(*) AS n FROM sources WHERE project_id = %s GROUP BY kind",
        (project_id,),
    )
    return {row["kind"]: int(row["n"]) for row in rows}


def get_source(source_id: str) -> dict | None:
    return db.fetch_one("SELECT * FROM sources WHERE id = %s", (source_id,))


def claim_next_pending_source() -> dict | None:
    """
    Take one pending source and mark it 'processing' in a single statement.

    FOR UPDATE SKIP LOCKED means that if two workers run at once, they will
    never pick the same source. This is the standard PostgreSQL job-queue
    pattern and avoids needing Redis or another queue service.
    """
    return db.fetch_one(
        """
        UPDATE sources s
        SET status = 'processing'
        FROM projects p
        WHERE s.id = (
            SELECT id FROM sources
            WHERE status = 'pending'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        AND p.id = s.project_id
        RETURNING s.*, p.ai_extraction_enabled
        """
    )


def finish_source(source_id: str, status: str, note: str = "") -> None:
    db.execute(
        "UPDATE sources SET status = %s, status_note = %s, processed_at = now() WHERE id = %s",
        (status, note, source_id),
    )


def reset_stuck_sources() -> int:
    """
    If a worker crashed mid-job, its source stays 'processing' forever.
    On worker start, put those back in the queue.
    """
    with db.transaction() as conn:
        cur = conn.execute("UPDATE sources SET status = 'pending' WHERE status = 'processing'")
        return cur.rowcount


# =====================================================================
# Events
# =====================================================================


def create_event(
    *,
    project_id: str,
    type_: str,
    summary: str,
    detail: str = "",
    owner: str | None = None,
    status: str = EventStatus.CONFIRMED.value,
    due_date: date | None = None,
    source_id: str | None = None,
    source_ref: str = "",
    related_id: str | None = None,
    proposed_by: str = "person",
    created_by: str | None = None,
) -> dict:
    """Insert one event and its first history row, in one transaction."""
    with db.transaction() as conn:
        event = conn.execute(
            """
            INSERT INTO events (project_id, type, summary, detail, owner, status, due_date,
                                source_id, source_ref, related_id, proposed_by, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (project_id, type_, summary.strip(), detail.strip(), owner or None, status,
             due_date, source_id, source_ref.strip(), related_id, proposed_by, created_by),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO event_history (event_id, old_status, new_status, note, changed_by)
            VALUES (%s, NULL, %s, %s, %s)
            """,
            (event["id"], status, "created", created_by),
        )
    return event


def insert_proposed_events(project_id: str, source: dict, proposals: Iterable[ProposedEvent]) -> int:
    """Save AI proposals for a source. They start as 'proposed' until a person reviews them."""
    count = 0
    for proposal in proposals:
        detail = proposal.detail
        if proposal.evidence:
            detail = (detail + "\n\nEvidence: " + proposal.evidence).strip()
        create_event(
            project_id=project_id,
            type_=proposal.type.value,
            summary=proposal.summary,
            detail=detail,
            owner=proposal.owner,
            status=EventStatus.PROPOSED.value,
            due_date=proposal.due_date,
            source_id=str(source["id"]),
            source_ref=source["filename"],
            proposed_by="ai",
            created_by=None,
        )
        count += 1
    return count


def get_event(event_id: str) -> dict | None:
    return db.fetch_one("SELECT * FROM events WHERE id = %s", (event_id,))


def change_event_status(
    event_id: str, new_status: str, changed_by: str | None, note: str = ""
) -> dict:
    """
    Change an event's status and record the change in event_history.
    The caller must already have checked models.can_change_status().
    """
    with db.transaction() as conn:
        # FOR UPDATE stops two people changing the same event at the same instant.
        current = conn.execute(
            "SELECT status FROM events WHERE id = %s FOR UPDATE", (event_id,)
        ).fetchone()
        event = conn.execute(
            "UPDATE events SET status = %s, updated_at = now() WHERE id = %s RETURNING *",
            (new_status, event_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO event_history (event_id, old_status, new_status, note, changed_by)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (event_id, current["status"], new_status, note, changed_by),
        )
    return event


def supersede_event(old_event_id: str, new_event: dict, changed_by: str | None) -> None:
    """Mark an old event as replaced by a newer one."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE events SET supersedes_id = %s WHERE id = %s", (old_event_id, new_event["id"])
        )
    change_event_status(old_event_id, EventStatus.SUPERSEDED.value, changed_by,
                        note=f"superseded by: {new_event['summary']}")


def event_history(event_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT h.*, u.display_name AS changed_by_name
        FROM event_history h
        LEFT JOIN users u ON u.id = h.changed_by
        WHERE h.event_id = %s
        ORDER BY h.changed_at
        """,
        (event_id,),
    )


def search_events(
    project_id: str,
    query: str = "",
    type_: str = "",
    status: str = "",
    limit: int = 50,
) -> list[dict]:
    """
    Search a project's events. Empty filters are ignored.
    Rejected events are hidden unless the caller asks for them by status.
    """
    conditions = ["e.project_id = %(project_id)s"]
    params: dict = {"project_id": project_id, "limit": limit}

    if query.strip():
        # websearch_to_tsquery accepts normal search text ("batch -serial")
        # and never raises a syntax error on user input.
        conditions.append("e.search @@ websearch_to_tsquery('simple', %(query)s)")
        params["query"] = query.strip()
    if type_:
        conditions.append("e.type = %(type)s")
        params["type"] = type_
    if status:
        conditions.append("e.status = %(status)s")
        params["status"] = status
    else:
        conditions.append("e.status <> 'rejected'")

    # The WHERE clause is built only from fixed strings above; user values
    # are always passed as parameters.
    sql = f"""
        SELECT e.*, s.filename AS source_filename
        FROM events e
        LEFT JOIN sources s ON s.id = e.source_id
        WHERE {" AND ".join(conditions)}
        ORDER BY e.created_at DESC
        LIMIT %(limit)s
    """
    return db.fetch_all(sql, params)


def events_for_drift(project_id: str) -> list[dict]:
    """Every event that drift rules might care about (rejected ones never matter)."""
    return db.fetch_all(
        "SELECT * FROM events WHERE project_id = %s AND status <> 'rejected'", (project_id,)
    )


def count_events_by_status(project_id: str) -> dict[str, int]:
    rows = db.fetch_all(
        "SELECT status, count(*) AS n FROM events WHERE project_id = %s GROUP BY status",
        (project_id,),
    )
    return {row["status"]: int(row["n"]) for row in rows}


# =====================================================================
# API tokens
# =====================================================================


def create_token(user_id: str, name: str, token_hash: str) -> dict:
    return db.fetch_one(
        "INSERT INTO api_tokens (user_id, name, token_hash) VALUES (%s, %s, %s) RETURNING id, name",
        (user_id, name.strip(), token_hash),
    )


def list_tokens(user_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT id, name, created_at, last_used_at, revoked_at
        FROM api_tokens WHERE user_id = %s ORDER BY created_at DESC
        """,
        (user_id,),
    )


def revoke_token(token_id: str, user_id: str) -> None:
    db.execute(
        "UPDATE api_tokens SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
        (token_id, user_id),
    )


def user_for_token(token_hash: str) -> dict | None:
    """Find the active user behind a token and record that the token was used."""
    return db.fetch_one(
        """
        UPDATE api_tokens t SET last_used_at = now()
        FROM users u
        WHERE t.token_hash = %s AND t.revoked_at IS NULL
          AND u.id = t.user_id AND u.is_active
        RETURNING u.id, u.email, u.display_name, u.is_admin, t.id AS token_id
        """,
        (token_hash,),
    )


# =====================================================================
# Audit log and app state
# =====================================================================


def audit(action: str, *, user_id=None, token_id=None, project_id=None, detail=None) -> None:
    db.execute(
        """
        INSERT INTO audit_log (user_id, token_id, project_id, action, detail)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        """,
        (user_id, token_id, project_id, action, json.dumps(detail or {}, default=str)),
    )


def get_state(key: str) -> str | None:
    row = db.fetch_one("SELECT value FROM app_state WHERE key = %s", (key,))
    return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO app_state (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (key, value),
    )


def all_project_ids() -> list[str]:
    return [str(row["id"]) for row in db.fetch_all("SELECT id FROM projects")]


def project_digest_recipients(project_id: str) -> list[dict]:
    """
    Owners receive the weekly digest; they are the ones expected to act on it.

    Owners who joined by name have no email address, so they are skipped here
    rather than handed to the mailer as a NULL recipient.
    """
    return db.fetch_all(
        """
        SELECT u.email, u.display_name
        FROM project_members m JOIN users u ON u.id = m.user_id
        WHERE m.project_id = %s AND m.role = 'owner' AND u.is_active
          AND u.email IS NOT NULL AND u.email <> ''
        """,
        (project_id,),
    )


# =====================================================================
# Identity without passwords
# =====================================================================


def create_named_user(display_name: str, is_admin: bool) -> dict:
    """
    Create a user who has only a display name.

    No email, no password: people join by typing a name once and are
    remembered by their session cookie. Roles still attach to this row, so
    permissions are enforced exactly as they are for a password account.
    """
    return db.fetch_one(
        """
        INSERT INTO users (email, display_name, password_hash, is_admin)
        VALUES (NULL, %s, NULL, %s)
        RETURNING *
        """,
        (display_name.strip(), is_admin),
    )


# =====================================================================
# Folders
#
# Which folders a new project starts with is decided by its project type,
# not here: see toogather/project_types.py.
# =====================================================================


def create_folder(project_id: str, name: str, slug: str, kind: str,
                  description: str, position: int, created_by: str | None) -> dict:
    return db.fetch_one(
        """
        INSERT INTO folders (project_id, name, slug, kind, description, position, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (project_id, name.strip(), slug, kind, description.strip(), position, created_by),
    )


def list_folders(project_id: str) -> list[dict]:
    """Folders with a live document count, for the sidebar."""
    return db.fetch_all(
        """
        SELECT f.*, count(d.id) AS doc_count
        FROM folders f
        LEFT JOIN documents d ON d.folder_id = f.id
        WHERE f.project_id = %s
        GROUP BY f.id
        ORDER BY f.position, f.name
        """,
        (project_id,),
    )


def get_folder(folder_id: str) -> dict | None:
    return db.fetch_one("SELECT * FROM folders WHERE id = %s", (folder_id,))


def delete_folder(folder_id: str) -> None:
    """Documents inside go with it (ON DELETE CASCADE)."""
    db.execute("DELETE FROM folders WHERE id = %s", (folder_id,))


def next_folder_position(project_id: str) -> int:
    row = db.fetch_one(
        "SELECT coalesce(max(position), 0) + 1 AS n FROM folders WHERE project_id = %s",
        (project_id,),
    )
    return int(row["n"]) if row else 100


def slug_is_taken(project_id: str, slug: str) -> bool:
    return db.fetch_one(
        "SELECT 1 AS x FROM folders WHERE project_id = %s AND slug = %s",
        (project_id, slug),
    ) is not None


# =====================================================================
# Documents
# =====================================================================


def create_document(project_id: str, folder_id: str | None, title: str,
                    body: str, doc_kind: str, created_by: str | None) -> dict:
    return db.fetch_one(
        """
        INSERT INTO documents (project_id, folder_id, title, body, doc_kind,
                               created_by, updated_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (project_id, folder_id, title.strip(), body, doc_kind, created_by, created_by),
    )


def get_document(document_id: str) -> dict | None:
    """One document, with the name of whoever saved it last."""
    return db.fetch_one(
        """
        SELECT d.*, u.display_name AS updated_by_name
        FROM documents d
        LEFT JOIN users u ON u.id = d.updated_by
        WHERE d.id = %s
        """,
        (document_id,),
    )


def list_documents(folder_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT id, title, doc_kind, updated_at
        FROM documents WHERE folder_id = %s
        ORDER BY updated_at DESC
        """,
        (folder_id,),
    )


def update_document(document_id: str, title: str, body: str, updated_by: str | None,
                    expected_updated_at: datetime | None = None) -> bool:
    """
    Save a document, keeping what it said before.

    Returns False when `expected_updated_at` is given and no longer matches,
    which means somebody else saved while this person was typing. Nothing is
    written in that case: the caller shows both versions rather than picking a
    winner silently, which is what "last write wins" did before.

    The previous text is copied into document_versions inside the same
    transaction, so a version row can never exist for a save that did not
    happen, nor a save happen without its version.
    """
    with db.transaction() as conn:
        current = conn.execute(
            "SELECT title, body, updated_at, updated_by FROM documents WHERE id = %s FOR UPDATE",
            (document_id,),
        ).fetchone()
        if current is None:
            return False
        if expected_updated_at is not None and current["updated_at"] != expected_updated_at:
            return False

        conn.execute(
            """
            INSERT INTO document_versions (document_id, title, body, edited_by)
            VALUES (%s, %s, %s, %s)
            """,
            (document_id, current["title"], current["body"], current["updated_by"]),
        )
        conn.execute(
            """
            UPDATE documents
            SET title = %s, body = %s, updated_by = %s, updated_at = now()
            WHERE id = %s
            """,
            (title.strip(), body, updated_by, document_id),
        )
    return True


def list_document_versions(document_id: str, limit: int = 50) -> list[dict]:
    """Previous versions, newest first, with who saved each one."""
    return db.fetch_all(
        """
        SELECT v.id, v.title, v.created_at, length(v.body) AS size,
               u.display_name AS edited_by_name
        FROM document_versions v
        LEFT JOIN users u ON u.id = v.edited_by
        WHERE v.document_id = %s
        ORDER BY v.id DESC
        LIMIT %s
        """,
        (document_id, limit),
    )


def get_document_version(version_id: str, document_id: str) -> dict | None:
    """One version. document_id is in the WHERE clause so ids cannot cross over."""
    return db.fetch_one(
        "SELECT * FROM document_versions WHERE id = %s AND document_id = %s",
        (version_id, document_id),
    )


def count_document_versions(document_id: str) -> int:
    row = db.fetch_one(
        "SELECT count(*) AS n FROM document_versions WHERE document_id = %s", (document_id,)
    )
    return int(row["n"]) if row else 0


def delete_document(document_id: str) -> None:
    db.execute("DELETE FROM documents WHERE id = %s", (document_id,))


def get_special_document(project_id: str, doc_kind: str) -> dict | None:
    """Fetch the project's single charter or skill document."""
    return db.fetch_one(
        "SELECT * FROM documents WHERE project_id = %s AND doc_kind = %s",
        (project_id, doc_kind),
    )


def recent_documents(project_id: str, limit: int = 8) -> list[dict]:
    return db.fetch_all(
        """
        SELECT d.id, d.title, d.doc_kind, d.updated_at, f.name AS folder_name
        FROM documents d LEFT JOIN folders f ON f.id = d.folder_id
        WHERE d.project_id = %s
        ORDER BY d.updated_at DESC
        LIMIT %s
        """,
        (project_id, limit),
    )


def search_documents(project_id: str, query: str, limit: int = 20) -> list[dict]:
    return db.fetch_all(
        """
        SELECT d.id, d.title, d.doc_kind, d.updated_at, f.name AS folder_name
        FROM documents d LEFT JOIN folders f ON f.id = d.folder_id
        WHERE d.project_id = %s
          AND d.search @@ plainto_tsquery('simple', %s)
        ORDER BY ts_rank(d.search, plainto_tsquery('simple', %s)) DESC
        LIMIT %s
        """,
        (project_id, query, query, limit),
    )


# =====================================================================
# Invites
# =====================================================================


def create_invite(project_id: str, code: str, role: str, label: str,
                  max_uses: int | None, created_by: str | None) -> dict:
    return db.fetch_one(
        """
        INSERT INTO invites (project_id, code, role, label, max_uses, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (project_id, code, role, label.strip(), max_uses, created_by),
    )


def list_invites(project_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT * FROM invites
        WHERE project_id = %s AND revoked_at IS NULL
        ORDER BY created_at DESC
        """,
        (project_id,),
    )


def get_invite_by_code(code: str) -> dict | None:
    return db.fetch_one("SELECT * FROM invites WHERE code = %s", (code,))


def revoke_invite(invite_id: str, project_id: str) -> None:
    """project_id is in the WHERE clause so one project cannot revoke another's invite."""
    db.execute(
        "UPDATE invites SET revoked_at = now() WHERE id = %s AND project_id = %s",
        (invite_id, project_id),
    )


def accept_invite(invite: dict, user_id: str) -> None:
    """
    Add the user to the project at the invite's role and count the use.

    Both statements run in one transaction so a half-accepted invite cannot
    exist. An existing member keeps their current role rather than being
    silently downgraded by re-opening an old link.
    """
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO project_members (project_id, user_id, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (project_id, user_id) DO NOTHING
            """,
            (invite["project_id"], user_id, invite["role"]),
        )
        conn.execute("UPDATE invites SET uses = uses + 1 WHERE id = %s", (invite["id"],))


def invite_problem(invite: dict | None) -> str | None:
    """Return why an invite cannot be used, or None if it is good."""
    if invite is None:
        return "That invite link is not valid."
    if invite["revoked_at"] is not None:
        return "That invite has been revoked."
    if invite["expires_at"] is not None and invite["expires_at"] < datetime.now(UTC):
        return "That invite has expired."
    if invite["max_uses"] is not None and invite["uses"] >= invite["max_uses"]:
        return "That invite has already been used the maximum number of times."
    return None


# =====================================================================
# Per-project AI provider settings
#
# A row here means "this project does not use the server's AI provider".
# No row means it does. The API key is stored encrypted; this module only
# moves the ciphertext around, and toogather/crypto.py is the only place
# that can read it.
# =====================================================================


def get_ai_settings(project_id: str) -> dict | None:
    return db.fetch_one(
        "SELECT * FROM project_ai_settings WHERE project_id = %s", (project_id,)
    )


def save_ai_settings(project_id: str, base_url: str, model: str,
                     api_key_encrypted: str | None, updated_by: str | None) -> None:
    """
    Save a project's AI provider.

    `api_key_encrypted` of None means "leave whatever key is stored alone",
    which is how the settings form can be submitted without the key being
    present in the page. An empty string clears it.
    """
    db.execute(
        """
        INSERT INTO project_ai_settings (project_id, base_url, model,
                                         api_key_encrypted, updated_by)
        VALUES (%s, %s, %s, coalesce(%s, ''), %s)
        ON CONFLICT (project_id) DO UPDATE SET
            base_url = EXCLUDED.base_url,
            model = EXCLUDED.model,
            api_key_encrypted = coalesce(%s, project_ai_settings.api_key_encrypted),
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        """,
        (project_id, base_url.strip(), model.strip(), api_key_encrypted, updated_by,
         api_key_encrypted),
    )


def clear_ai_settings(project_id: str) -> None:
    """Go back to the server's AI provider. The stored key is destroyed with the row."""
    db.execute("DELETE FROM project_ai_settings WHERE project_id = %s", (project_id,))


# =====================================================================
# Connectors
# =====================================================================


def create_connector(project_id: str, kind: str, name: str, config: dict,
                     secret_encrypted: str, run_every_minutes: int,
                     created_by: str | None) -> dict:
    return db.fetch_one(
        """
        INSERT INTO connectors (project_id, kind, name, config, secret_encrypted,
                                run_every_minutes, created_by)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
        RETURNING *
        """,
        (project_id, kind, name.strip(), json.dumps(config), secret_encrypted,
         run_every_minutes, created_by),
    )


def list_connectors(project_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT c.*, (SELECT count(*) FROM sources s WHERE s.connector_id = c.id) AS source_count
        FROM connectors c
        WHERE c.project_id = %s
        ORDER BY c.created_at
        """,
        (project_id,),
    )


def get_connector(connector_id: str) -> dict | None:
    return db.fetch_one("SELECT * FROM connectors WHERE id = %s", (connector_id,))


def update_connector(connector_id: str, project_id: str, name: str, config: dict,
                     secret_encrypted: str | None, run_every_minutes: int,
                     enabled: bool) -> None:
    """
    Save a connector's settings.

    `secret_encrypted` of None keeps the stored secret, the same convention as
    the AI key: the form never carries a secret it already holds.

    project_id is in the WHERE clause so a request cannot reach across into
    another project's connector by guessing an id.
    """
    db.execute(
        """
        UPDATE connectors
        SET name = %s,
            config = %s::jsonb,
            secret_encrypted = coalesce(%s, secret_encrypted),
            run_every_minutes = %s,
            enabled = %s
        WHERE id = %s AND project_id = %s
        """,
        (name.strip(), json.dumps(config), secret_encrypted, run_every_minutes,
         enabled, connector_id, project_id),
    )


def delete_connector(connector_id: str, project_id: str) -> None:
    """
    Remove a connector. Sources it brought in stay, with connector_id set to
    NULL, because the events made from them are still true.
    """
    db.execute(
        "DELETE FROM connectors WHERE id = %s AND project_id = %s", (connector_id, project_id)
    )


def run_connector_now(connector_id: str, project_id: str) -> None:
    """Make a connector due immediately, for the "Check now" button."""
    db.execute(
        """
        UPDATE connectors SET last_run_at = NULL
        WHERE id = %s AND project_id = %s AND last_status <> 'running'
        """,
        (connector_id, project_id),
    )


def claim_due_connector() -> dict | None:
    """
    Take the connector that is most overdue and mark it 'running'.

    The same FOR UPDATE SKIP LOCKED pattern as the source queue, for the same
    reason: two workers must never run one connector at the same moment, and
    the database is a good enough queue that no other service is needed.

    A connector that has never run (last_run_at IS NULL) goes first. An
    inbound connector is never claimed: it has nothing to check, and marking
    it "running" would leave it reporting a failure it was not asked for.
    """
    return db.fetch_one(
        """
        UPDATE connectors c
        SET last_status = 'running', last_run_at = now()
        WHERE c.id = (
            SELECT id FROM connectors
            WHERE enabled
              AND polls
              AND last_status <> 'running'
              AND (last_run_at IS NULL
                   OR last_run_at < now() - make_interval(mins => run_every_minutes))
            ORDER BY last_run_at NULLS FIRST
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING c.*
        """
    )


def finish_connector(connector_id: str, status: str, note: str, cursor: str | None) -> None:
    """
    Record how a run went.

    `cursor` of None leaves the stored cursor alone, which is what a failed run
    wants: the next run should try the same range again rather than skipping it.
    """
    db.execute(
        """
        UPDATE connectors
        SET last_status = %s, last_note = %s, cursor = coalesce(%s, cursor)
        WHERE id = %s
        """,
        (status, note[:1000], cursor, connector_id),
    )


def release_running_connectors() -> int:
    """
    If a worker died mid-run, its connector stays 'running' forever.
    On worker start, put those back to a state that can be claimed again.
    """
    with db.transaction() as conn:
        cur = conn.execute(
            """
            UPDATE connectors
            SET last_status = 'failed',
                last_note = 'The worker stopped during this run. It will be retried.'
            WHERE last_status = 'running'
            """
        )
        return cur.rowcount


# =====================================================================
# Event text, and the revisions of it
#
# Status changes live in event_history; the words live here. They answer
# different questions - "who agreed this was true?" and "who changed what
# it says?" - and keeping them apart keeps both readable.
# =====================================================================


def update_event_text(
    event_id: str,
    *,
    summary: str,
    detail: str,
    owner: str | None,
    due_date: date | None,
    source_ref: str,
    edited_by: str | None,
) -> dict | None:
    """
    Correct what an event says, keeping what it said before.

    Only the words change: an event's status, its links and its source are
    untouched, so correcting a typo in a confirmed decision does not quietly
    re-open it.
    """
    with db.transaction() as conn:
        current = conn.execute(
            "SELECT * FROM events WHERE id = %s FOR UPDATE", (event_id,)
        ).fetchone()
        if current is None:
            return None

        unchanged = (
            current["summary"] == summary.strip()
            and current["detail"] == detail.strip()
            and (current["owner"] or "") == (owner or "")
            and current["due_date"] == due_date
            and current["source_ref"] == source_ref.strip()
        )
        if unchanged:
            return current      # nothing to record, and no empty revision row

        conn.execute(
            """
            INSERT INTO event_revisions
                (event_id, summary, detail, owner, due_date, source_ref, edited_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (event_id, current["summary"], current["detail"], current["owner"],
             current["due_date"], current["source_ref"], edited_by),
        )
        return conn.execute(
            """
            UPDATE events
            SET summary = %s, detail = %s, owner = %s, due_date = %s,
                source_ref = %s, updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (summary.strip(), detail.strip(), owner or None, due_date,
             source_ref.strip(), event_id),
        ).fetchone()


def event_revisions(event_id: str, limit: int = 50) -> list[dict]:
    """What this event used to say, newest first."""
    return db.fetch_all(
        """
        SELECT r.*, u.display_name AS edited_by_name
        FROM event_revisions r
        LEFT JOIN users u ON u.id = r.edited_by
        WHERE r.event_id = %s
        ORDER BY r.id DESC
        LIMIT %s
        """,
        (event_id, limit),
    )


def events_for_bulk_review(project_id: str, event_ids: list[str]) -> list[dict]:
    """
    The subset of these ids that really belong to this project.

    The ids come from checkboxes on a page, so they are caller-supplied. This
    is what stops a crafted form from moving an event in somebody else's
    project: anything not in this project simply is not returned.
    """
    if not event_ids:
        return []
    return db.fetch_all(
        "SELECT id, status, summary FROM events WHERE project_id = %s AND id = ANY(%s)",
        (project_id, event_ids),
    )


# =====================================================================
# Connectors, continued
# =====================================================================


def create_inbound_connector(project_id: str, kind: str, name: str, config: dict,
                             code: str, created_by: str | None) -> dict:
    """
    A connector that is posted to rather than checked.

    `polls` is FALSE so the worker never claims it, and the code is what its
    URL carries. Stored in plain text on purpose, like an invite code: an
    owner has to be able to read it back to paste it into whatever calls it.
    """
    return db.fetch_one(
        """
        INSERT INTO connectors (project_id, kind, name, config, polls, inbound_code,
                                run_every_minutes, created_by)
        VALUES (%s, %s, %s, %s::jsonb, FALSE, %s, 0, %s)
        RETURNING *
        """,
        (project_id, kind, name.strip(), json.dumps(config), code, created_by),
    )


def get_connector_by_code(code: str) -> dict | None:
    """Find an enabled inbound connector from the code in its URL."""
    return db.fetch_one(
        """
        SELECT * FROM connectors
        WHERE inbound_code = %s AND NOT polls AND enabled
        """,
        (code,),
    )


def record_inbound(connector_id: str, note: str) -> None:
    """Note that an inbound connector was called, without claiming it ran."""
    db.execute(
        """
        UPDATE connectors
        SET last_run_at = now(), last_status = 'ok', last_note = %s
        WHERE id = %s
        """,
        (note[:1000], connector_id),
    )


def create_inbound_source(project_id: str, connector_id: str,
                          filename: str, content: str) -> dict:
    """
    Store a note posted to an inbound connector.

    No external_id, unlike material a polling connector brings in: a webhook
    has no stable identifier for what it sends, and posting the same summary
    twice is a legitimate thing to do. The unique index covers only a non-empty
    external_id, so leaving it blank makes each delivery its own source instead
    of the second one being refused.

    uploaded_by is NULL because nobody was signed in - the connector is the
    provenance, and the sources page shows it as such.
    """
    return db.fetch_one(
        """
        INSERT INTO sources (project_id, kind, filename, content, connector_id)
        VALUES (%s, 'connector', %s, %s, %s)
        RETURNING id, filename, status, created_at
        """,
        (project_id, filename[:200], content, connector_id),
    )
