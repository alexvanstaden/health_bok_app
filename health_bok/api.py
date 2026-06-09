"""The Python HTTP API over the health_bok domain (ADR-0009).

A thin FastAPI surface the Next.js Web App calls. It owns no business logic — it
reuses the same `Repository` and review/worker services the daily pipeline uses,
against the one Postgres (ADR-0003), so the Web App and the pipeline never drift.
Approval enqueues a job and returns immediately; the worker does the slow work
(ADR-0009).

Auth is the tailnet, not a login screen (ADR-0009): the API is only ever reached
over Tailscale. CORS is permissive because there is no cross-origin threat model
behind the tailnet.

Run it with: `uvicorn health_bok.api:app` (the docker `api` service does this).
FastAPI is imported at module load, so this module is only imported by that
entrypoint — never by the domain or the test suite.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import config, review
from .db import connect, init_schema
from .repository import Repository

app = FastAPI(title="Health & Longevity Knowledge — Web App API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # behind Tailscale; no cross-origin threat model (ADR-0009)
    allow_methods=["*"],
    allow_headers=["*"],
)

_DATABASE_URL: str | None = None


@app.on_event("startup")
def _startup() -> None:
    """Resolve the DB URL and apply the schema once (idempotent, ADR-0003)."""
    global _DATABASE_URL
    _DATABASE_URL = config.database_url()
    conn = connect(_DATABASE_URL)
    try:
        init_schema(conn)
    finally:
        conn.close()


@contextmanager
def _repo() -> Iterator[Repository]:
    """A Repository on a fresh connection, closed when the request ends.

    Single-user, self-hosted scale (ADR-0009): a connection per request is plenty
    and keeps the request's transaction boundary obvious — the review services
    commit; this just guarantees the connection is released.
    """
    conn = connect(_DATABASE_URL or config.database_url())
    try:
        yield Repository(conn)
    finally:
        conn.close()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/candidates")
def list_candidates() -> dict:
    """The daily review queue: Candidates with their Summary and state (ADR-0007)."""
    with _repo() as repo:
        candidates = repo.list_daily_candidates()
    return {
        "candidates": [
            {
                "video_id": c.video_id,
                "title": c.title,
                "url": c.url,
                "summary": c.summary,
                "state": c.state,
                "published_at": c.published_at.isoformat(),
            }
            for c in candidates
        ]
    }


@app.post("/api/candidates/{video_id}/approve")
def approve(video_id: str) -> dict:
    """Approve a Candidate; enqueues the admission job and returns at once."""
    with _repo() as repo:
        enqueued = review.approve_candidate(video_id, repo=repo)
    return {"video_id": video_id, "enqueued": enqueued, "state": "approved"}


@app.post("/api/candidates/{video_id}/reject")
def reject(video_id: str) -> dict:
    """Reject a Candidate, removing it from the queue without admitting it."""
    with _repo() as repo:
        rejected = review.reject_candidate(video_id, repo=repo)
    if not rejected:
        raise HTTPException(status_code=409, detail="already admitted")
    return {"video_id": video_id, "state": "rejected"}


@app.post("/api/candidates/{video_id}/retry")
def retry(video_id: str) -> dict:
    """Retry a Candidate whose extraction failed."""
    with _repo() as repo:
        retried = review.retry_candidate(video_id, repo=repo)
    if not retried:
        raise HTTPException(status_code=409, detail="not in a failed state")
    return {"video_id": video_id, "enqueued": True, "state": "approved"}


@app.get("/api/videos/{video_id}/claims")
def video_claims(video_id: str) -> dict:
    """A video's admitted Claims and Protocols, each with a locator deep-link."""
    with _repo() as repo:
        state = repo.admission_state(video_id)
        claims = repo.admitted_claims(video_id)
        protocols = repo.admitted_protocols(video_id)
    return {
        "video_id": video_id,
        "state": state,
        "claims": [
            {
                "id": c.id,
                "text": c.text,
                "type": c.type,
                "locator_seconds": c.locator_seconds,
                "deep_link": c.deep_link,
                "concepts": c.concepts,
            }
            for c in claims
        ],
        "protocols": [
            {
                "id": p.id,
                "action": p.action,
                "dose": p.dose,
                "timing": p.timing,
                "frequency": p.frequency,
                "duration": p.duration,
                "locator_seconds": p.locator_seconds,
                "deep_link": p.deep_link,
                "concepts": p.concepts,
            }
            for p in protocols
        ],
    }
