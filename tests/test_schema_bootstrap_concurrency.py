"""Concurrent schema bootstrap — the api/worker/pipeline startup race.

Every long-running service applies the schema on boot against the one Postgres
(ADR-0003). On a fresh database the simultaneous DDL collided with Postgres'
"tuple concurrently updated" on the shared catalogs, crashing whichever service
lost the race — notably the restart-less `worker`, which then never drained the
admission queue. `init_schema` must serialize itself so concurrent bootstraps are
safe.
"""

from __future__ import annotations

import threading

import psycopg

from health_bok.db import init_schema


def test_concurrent_init_schema_is_safe(postgres_url):
    # Fresh database, so every thread races the *full* DDL (extension, types,
    # tables, indexes) — the conditions that triggered the catalog collision.
    reset = psycopg.connect(postgres_url)
    with reset.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    reset.commit()
    reset.close()

    errors: list[Exception] = []
    barrier = threading.Barrier(5)

    def bootstrap() -> None:
        conn = psycopg.connect(postgres_url)
        try:
            barrier.wait()  # release all threads into the DDL at once
            init_schema(conn)
        except Exception as exc:  # noqa: BLE001 - captured for the assertion
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=bootstrap) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"concurrent init_schema raised: {errors!r}"
