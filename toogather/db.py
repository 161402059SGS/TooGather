"""
Database access helpers.

We use plain SQL with psycopg instead of an ORM. The SQL in this project is
simple, and plain SQL is easier to read, debug in psql, and review for
security. Every query uses parameters (%s placeholders), never string
formatting, so user input can never change the SQL itself.

Migrations are plain .sql files in toogather/migrations, applied in file-name
order at startup. Each applied file is recorded in schema_migrations so it
never runs twice.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("toogather.db")

# A single pool shared by the whole process. Created by init_pool().
_pool: ConnectionPool | None = None


def init_pool(database_url: str, max_size: int = 10) -> None:
    """Open the connection pool. Call once when the process starts."""
    global _pool
    if _pool is None:
        # dict_row makes every result row a dict, so code reads row["summary"]
        # instead of row[3], which is much harder to follow.
        _pool = ConnectionPool(
            database_url,
            min_size=1,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def transaction() -> Iterator[Any]:
    """
    Borrow a connection and run everything inside one transaction.

    Usage:
        with transaction() as conn:
            conn.execute("UPDATE ...", (value,))

    The transaction commits when the block ends normally and rolls back if an
    exception is raised, so a half-finished change is never saved.
    """
    if _pool is None:
        raise RuntimeError("Database pool is not initialised. Call init_pool() first.")
    with _pool.connection() as conn, conn.transaction():
        yield conn


def fetch_all(sql: str, params: tuple | dict = ()) -> list[dict]:
    with transaction() as conn:
        return conn.execute(sql, params).fetchall()


def fetch_one(sql: str, params: tuple | dict = ()) -> dict | None:
    with transaction() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: tuple | dict = ()) -> None:
    with transaction() as conn:
        conn.execute(sql, params)


def run_migrations() -> list[str]:
    """
    Apply any migration files that have not run yet. Returns the names applied.

    A PostgreSQL advisory lock makes this safe when the web app and the worker
    start at the same moment: only one process migrates, the other waits.
    """
    applied_now: list[str] = []
    migration_files = sorted(
        (entry for entry in resources.files("toogather.migrations").iterdir()
         if entry.name.endswith(".sql")),
        key=lambda entry: entry.name,
    )

    with transaction() as conn:
        # Arbitrary fixed number identifying "TooGather migrations".
        conn.execute("SELECT pg_advisory_xact_lock(724201)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}

        for entry in migration_files:
            if entry.name in done:
                continue
            log.info("Applying migration %s", entry.name)
            conn.execute(entry.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (entry.name,))
            applied_now.append(entry.name)

    return applied_now
