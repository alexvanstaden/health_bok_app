"""Postgres connection and schema bootstrap.

The schema lives in `schema.sql` and is applied idempotently, so the same call
initializes a fresh deploy or a throwaway test database.
"""

from __future__ import annotations

from importlib import resources

import psycopg

SCHEMA_RESOURCE = "schema.sql"

# A fixed key for the advisory lock that serializes schema bootstrap. All services
# (api, worker, pipeline) apply the schema on boot against the one Postgres; the
# DDL is idempotent but not atomic against itself, so concurrent boots collided on
# the shared catalogs ("tuple concurrently updated" / duplicate-key on pg_type).
# Holding this lock makes the bootstraps run one at a time instead. Arbitrary
# constant — only its stability across processes matters.
_SCHEMA_LOCK_KEY = 0x6B6F6B5F696E6974  # "kok_init"


def connect(database_url: str) -> psycopg.Connection:
    """Open a connection to the single source-of-truth Postgres (ADR-0003)."""
    return psycopg.connect(database_url)


def load_schema_sql() -> str:
    """Return the DDL packaged alongside this module."""
    return resources.files("health_bok").joinpath(SCHEMA_RESOURCE).read_text()


def init_schema(conn: psycopg.Connection) -> None:
    """Apply the schema. Idempotent and safe under concurrent startups.

    A transaction-scoped advisory lock serializes the DDL across processes: the
    first boot applies the schema while the others block, then each runs its now
    no-op DDL in turn. The lock releases automatically when the transaction ends,
    so a crash mid-bootstrap never strands it. Without this, simultaneous boots of
    the api/worker/pipeline raced on the catalogs and crashed the loser.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
        cur.execute(load_schema_sql())
    conn.commit()
