"""Postgres connection and schema bootstrap.

The schema lives in `schema.sql` and is applied idempotently, so the same call
initializes a fresh deploy or a throwaway test database.
"""

from __future__ import annotations

from importlib import resources

import psycopg

SCHEMA_RESOURCE = "schema.sql"


def connect(database_url: str) -> psycopg.Connection:
    """Open a connection to the single source-of-truth Postgres (ADR-0003)."""
    return psycopg.connect(database_url)


def load_schema_sql() -> str:
    """Return the DDL packaged alongside this module."""
    return resources.files("health_bok").joinpath(SCHEMA_RESOURCE).read_text()


def init_schema(conn: psycopg.Connection) -> None:
    """Apply the schema. Idempotent — safe on every startup."""
    with conn.cursor() as cur:
        cur.execute(load_schema_sql())
    conn.commit()
