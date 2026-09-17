"""
All SQL queries used by TooGather, grouped by subject.

Why one file: when someone needs to know "what touches the events table?",
they can search one place. Every query uses %s parameters, so user input is
always sent separately from the SQL text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date

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
    return db.fetch_one(
        "SELECT id, email, display_name, is_admin FROM users WHERE id = %s AND is_active",
        (user_id,),
    )


def list_users() -> list[dict]:
    return db.fetch_all(
        "SELECT id, email, display_name, is_admin, is_active, created_at FROM users ORDER BY email"
    )


# =====================================================================
# Projects and membership
# =====================================================================


def create_project(name: str, description: str, created_by: str) -> dict:
    """Create a project and make its creator the owner, in one transaction."""
    with db.transaction() as conn:
        project = conn.execute(
            """
            INSERT INTO projects (name, description, created_by)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (name.strip(), description.strip(), created_by),
        ).fetchone()
        conn.execute(
            "INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, %s)",
            (project["id"], created_by, Role.OWNER.value),
        )
    return project


def list_projects_for_user(user: dict) -> list[dict]:
    """Admins see every project; everyone else sees projects they belong to."""
    if user["is_admin"]:
        return db.fetch_all("SELECT *, 'admin' AS my_role FROM projects ORDER BY name")
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


def get_project(project_id: str) -> dict | None:
    return db.fetch_one("SELECT * FROM projects WHERE id = %s", (project_id,))


def get_role(project_id: str, user: dict) -> str | None:
    """
    Return the user's role in a project, or None if they have no access.
    Admins are treated as owners of every project.
    """
    if user["is_admin"]:
        return Role.OWNER.value
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
        SELECT u.id, u.email, u.display_name, m.role
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


def list_sources(project_id: str, limit: int = 20) -> list[dict]:
    return db.fetch_all(
        """
        SELECT s.id, s.filename, s.status, s.status_note, s.created_at, s.processed_at,
               u.display_name AS uploaded_by_name,
               (SELECT count(*) FROM events e WHERE e.source_id = s.id) AS event_count
        FROM sources s
        LEFT JOIN users u ON u.id = s.uploaded_by
        WHERE s.project_id = %s
        ORDER BY s.created_at DESC
        LIMIT %s
        """,
        (project_id, limit),
    )


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
    """Owners receive the weekly digest; they are the ones expected to act on it."""
    return db.fetch_all(
        """
        SELECT u.email, u.display_name
        FROM project_members m JOIN users u ON u.id = m.user_id
        WHERE m.project_id = %s AND m.role = 'owner' AND u.is_active
        """,
        (project_id,),
    )
