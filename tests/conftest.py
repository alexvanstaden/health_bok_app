"""Pytest fixtures: a real ephemeral Postgres for the integration test.

A single throwaway Postgres container is started for the session; each test gets
a clean schema. This realizes the PRD's "real Postgres, not a port to mock"
decision (PRD #1).
"""

from __future__ import annotations

import re

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from health_bok.db import init_schema

# pgvector is required by the Part-2 knowledge schema (ADR-0008): the `vector`
# extension and HNSW index don't exist in the stock Postgres image. This image
# ships them, and is the same family deployed via docker-compose.
POSTGRES_IMAGE = "pgvector/pgvector:pg16"


def _psycopg3_url(raw_url: str) -> str:
    """Normalize testcontainers' SQLAlchemy-style URL for psycopg3.

    testcontainers returns e.g. `postgresql+psycopg2://...`; psycopg wants a
    plain `postgresql://...`.
    """
    return re.sub(r"^postgresql\+\w+://", "postgresql://", raw_url)


@pytest.fixture(scope="session")
def postgres_url():
    with PostgresContainer(POSTGRES_IMAGE) as container:
        yield _psycopg3_url(container.get_connection_url())


@pytest.fixture()
def conn(postgres_url):
    """A connection to a freshly-schema'd database, isolated per test."""
    connection = psycopg.connect(postgres_url)
    with connection.cursor() as cur:
        # Clean slate between tests.
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    connection.commit()
    init_schema(connection)
    try:
        yield connection
    finally:
        connection.close()
