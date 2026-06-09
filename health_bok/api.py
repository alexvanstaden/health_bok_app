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
from pydantic import BaseModel

from . import config, creators, curation, review
from .adapters.youtube import YouTubeContentSource
from .db import connect, init_schema
from .models import CreatorResolutionError
from .repository import (
    BokClaim,
    BokConcept,
    BokProtocol,
    Repository,
)

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


# == Creator management & backfill (issue #15) =============================
#
# Maintain the watch list and pull in a Creator's back-catalogue from the Web App,
# so the owner never needs the CLI to feed the pipeline (ADR-0009). Add reuses the
# existing `resolve_creator` path (resolve once, persist the stable channel_id) and
# seeds the recent back-catalogue as metadata-only Candidates; an explicit backfill
# trigger re-runs that population on demand. Approving a backfill Candidate uses the
# very same `/approve` endpoint as a daily one — the worker then transcribes-if-
# needed before extracting (issue #15). Bulk-reject clears obvious noise in one go.


class AddCreator(BaseModel):
    """A reference to add to the watch list — an @handle, bare handle, or URL."""

    reference: str


class BulkReject(BaseModel):
    """The backfill Candidate video_ids the owner is rejecting as noise."""

    video_ids: list[str]


@app.get("/api/creators")
def list_creators() -> dict:
    """The watch list: each Creator with its resolved channel name (issue #15)."""
    with _repo() as repo:
        watched = repo.list_creators()
    return {
        "creators": [
            {"channel_id": c.channel_id, "name": c.name} for c in watched
        ]
    }


@app.post("/api/creators")
def add_creator(body: AddCreator) -> dict:
    """Add a Creator by @handle or channel URL; an unresolvable one fails loudly.

    Resolves the reference once via YouTube, persists the stable identity, and
    seeds its recent back-catalogue as metadata-only Candidates (issue #7), all in
    one transaction. A reference that names no reachable channel returns 422.
    """
    with _repo() as repo:
        try:
            identity = creators.add_creator(
                body.reference,
                content_source=YouTubeContentSource(),
                repo=repo,
                cutoff=config.backfill_cutoff(),
            )
        except CreatorResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"channel_id": identity.channel_id, "name": identity.name}


@app.delete("/api/creators/{channel_id}")
def remove_creator(channel_id: str) -> dict:
    """Remove a Creator from the watch list by its stable channel_id."""
    with _repo() as repo:
        removed = creators.remove_creator(channel_id, repo=repo)
    if not removed:
        raise HTTPException(status_code=404, detail="creator not found")
    return {"channel_id": channel_id, "removed": True}


@app.post("/api/creators/{channel_id}/backfill")
def trigger_backfill(channel_id: str) -> dict:
    """Trigger a backfill of a watched Creator's back-catalogue (issue #15).

    Surfaces the Creator's recent back-catalogue as metadata-only Candidates;
    idempotent, so it only tops up newly-seen uploads. 404 if the channel_id is
    not on the watch list.
    """
    with _repo() as repo:
        stored = creators.backfill_creator(
            channel_id,
            content_source=YouTubeContentSource(),
            repo=repo,
            cutoff=config.backfill_cutoff(),
        )
    if stored is None:
        raise HTTPException(status_code=404, detail="creator not found")
    return {"channel_id": channel_id, "stored": stored, "count": len(stored)}


@app.get("/api/backfill")
def list_backfill() -> dict:
    """The backfill review queue: metadata-only Candidates awaiting a decision."""
    with _repo() as repo:
        candidates = repo.list_backfill_candidates()
    return {
        "candidates": [
            {
                "video_id": c.video_id,
                "title": c.title,
                "description": c.description,
                "url": c.url,
                "thumbnail_url": c.thumbnail_url,
                "published_at": c.published_at.isoformat(),
                "channel_id": c.channel_id,
                "channel_name": c.channel_name,
                "state": c.state,
            }
            for c in candidates
        ]
    }


@app.post("/api/backfill/reject")
def reject_backfill(body: BulkReject) -> dict:
    """Bulk-reject obvious backfill noise; returns how many were rejected."""
    with _repo() as repo:
        rejected = review.bulk_reject(body.video_ids, repo=repo)
    return {"rejected": rejected}


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


# == Body of Knowledge browser (issue #14) ==================================
#
# The browsable, editable evidence layer (ADR-0009 "no visual graph", ADR-0010).
# Browse and detail are plain repository reads; edit/delete go through the
# `curation` service, which owns the transaction and protects an edited entity.
# Detail reads serialize the connections (referenced Concepts, supported
# Protocols, justifying Claims) the Web App turns into navigable links.


class ClaimEdit(BaseModel):
    """An owner's in-place edit to a Claim — the editable fields (ADR-0010)."""

    text: str
    type: str
    locator_seconds: int


class ProtocolEdit(BaseModel):
    """An owner's in-place edit to a Protocol; the DB CHECK still enforces that at
    least one of dose/timing/frequency/duration survives (ADR-0010)."""

    action: str
    dose: str | None = None
    timing: str | None = None
    frequency: str | None = None
    duration: str | None = None
    locator_seconds: int


def _claim_dict(c: BokClaim) -> dict:
    return {
        "id": c.id,
        "text": c.text,
        "type": c.type,
        "locator_seconds": c.locator_seconds,
        "deep_link": c.deep_link,
        "protected": c.protected,
        "source": {"video_id": c.source_video_id, "title": c.source_title},
        "concepts": [{"id": r.id, "name": r.name} for r in c.concepts],
        "supports": [{"id": r.id, "action": r.action} for r in c.supports],
    }


def _protocol_dict(p: BokProtocol) -> dict:
    return {
        "id": p.id,
        "action": p.action,
        "dose": p.dose,
        "timing": p.timing,
        "frequency": p.frequency,
        "duration": p.duration,
        "locator_seconds": p.locator_seconds,
        "deep_link": p.deep_link,
        "protected": p.protected,
        "source": {"video_id": p.source_video_id, "title": p.source_title},
        "concepts": [{"id": r.id, "name": r.name} for r in p.concepts],
        "justified_by": [{"id": r.id, "text": r.text} for r in p.justified_by],
    }


def _concept_dict(c: BokConcept) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "kind": c.kind,
        "reference_count": c.reference_count,
        "claims": [{"id": r.id, "text": r.text} for r in c.claims],
        "protocols": [{"id": r.id, "action": r.action} for r in c.protocols],
    }


@app.get("/api/claims")
def browse_claims(concept_id: int | None = None, type: str | None = None) -> dict:
    """Filterable list of admitted Claims (by referenced Concept and/or sub-kind)."""
    with _repo() as repo:
        claims = repo.list_claims(concept_id=concept_id, type=type)
    return {"claims": [_claim_dict(c) for c in claims]}


@app.get("/api/claims/{claim_id}")
def claim_detail(claim_id: int) -> dict:
    """One Claim with its Source, referenced Concepts, and supported Protocols."""
    with _repo() as repo:
        claim = repo.get_claim(claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="claim not found")
    return _claim_dict(claim)


@app.patch("/api/claims/{claim_id}")
def edit_claim(claim_id: int, body: ClaimEdit) -> dict:
    """Edit a Claim in place; the edit is recorded as a protected version (ADR-0010)."""
    with _repo() as repo:
        edited = curation.edit_claim(
            claim_id,
            text=body.text,
            type=body.type,
            locator_seconds=body.locator_seconds,
            repo=repo,
        )
    if not edited:
        raise HTTPException(status_code=404, detail="claim not found")
    return {"id": claim_id, "protected": True}


@app.delete("/api/claims/{claim_id}")
def delete_claim(claim_id: int) -> dict:
    """Delete a Claim and the edges hanging off it (issue #14)."""
    with _repo() as repo:
        deleted = curation.delete_claim(claim_id, repo=repo)
    if not deleted:
        raise HTTPException(status_code=404, detail="claim not found")
    return {"id": claim_id, "deleted": True}


@app.get("/api/protocols")
def browse_protocols(concept_id: int | None = None) -> dict:
    """Filterable list of admitted Protocols (by referenced Concept)."""
    with _repo() as repo:
        protocols = repo.list_protocols(concept_id=concept_id)
    return {"protocols": [_protocol_dict(p) for p in protocols]}


@app.get("/api/protocols/{protocol_id}")
def protocol_detail(protocol_id: int) -> dict:
    """One Protocol with its Source, referenced Concepts, and justifying Claims."""
    with _repo() as repo:
        protocol = repo.get_protocol(protocol_id)
    if protocol is None:
        raise HTTPException(status_code=404, detail="protocol not found")
    return _protocol_dict(protocol)


@app.patch("/api/protocols/{protocol_id}")
def edit_protocol(protocol_id: int, body: ProtocolEdit) -> dict:
    """Edit a Protocol in place; the edit is recorded as a protected version (ADR-0010)."""
    with _repo() as repo:
        edited = curation.edit_protocol(
            protocol_id,
            action=body.action,
            dose=body.dose,
            timing=body.timing,
            frequency=body.frequency,
            duration=body.duration,
            locator_seconds=body.locator_seconds,
            repo=repo,
        )
    if not edited:
        raise HTTPException(status_code=404, detail="protocol not found")
    return {"id": protocol_id, "protected": True}


@app.delete("/api/protocols/{protocol_id}")
def delete_protocol(protocol_id: int) -> dict:
    """Delete a Protocol and the edges hanging off it (issue #14)."""
    with _repo() as repo:
        deleted = curation.delete_protocol(protocol_id, repo=repo)
    if not deleted:
        raise HTTPException(status_code=404, detail="protocol not found")
    return {"id": protocol_id, "deleted": True}


@app.get("/api/concepts")
def browse_concepts(kind: str | None = None) -> dict:
    """Filterable list of Concept hub nodes with their reference counts."""
    with _repo() as repo:
        concepts = repo.list_concepts(kind=kind)
    return {"concepts": [_concept_dict(c) for c in concepts]}


@app.get("/api/concepts/{concept_id}")
def concept_detail(concept_id: int) -> dict:
    """One Concept with the Claims and Protocols that reference it."""
    with _repo() as repo:
        concept = repo.get_concept(concept_id)
    if concept is None:
        raise HTTPException(status_code=404, detail="concept not found")
    return _concept_dict(concept)
