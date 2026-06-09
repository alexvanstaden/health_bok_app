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

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config, creators, curation, impacts, personal, query, review
from .adapters.answerer import ClaudeQueryAnswerer
from .adapters.embedder import OpenAIEmbedder
from .adapters.stance import ClaudeStanceJudge
from .adapters.youtube import YouTubeContentSource
from .concepts import ConceptNormalizer
from .db import connect, init_schema
from .models import CreatorResolutionError
from .personal import UnknownLinkTarget
from .repository import (
    BokClaim,
    BokConcept,
    BokProtocol,
    Decision,
    Goal,
    Impact,
    MarkerReading,
    MarkerSeries,
    Repository,
    SuggestedLink,
)

logger = logging.getLogger("health_bok.api")

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


def _normalizer(repo: Repository) -> ConceptNormalizer:
    """A ConceptNormalizer wired to the real OpenAI Embedder (ADR-0008).

    Recording a Goal/Marker/Decision resolves its Concept mentions onto the *same*
    canonical Concepts the admit pipeline mints, so personal-layer Concept overlap
    is meaningful (issue #16). Mirrors the worker's wiring (main.py)."""
    model = config.embedding_model()
    return ConceptNormalizer(
        OpenAIEmbedder(config.openai_api_key(), model),
        repo,
        model=model,
        merge_distance=config.concept_merge_distance(),
    )


def _detect_anchor_impacts(repo: Repository, anchor_type: str, anchor_id: int) -> None:
    """Run the reverse Impact pass for a just-recorded Decision/Goal (issue #18).

    Best-effort and synchronous (like `/api/query`'s Claude call): the anchor is
    already committed, so a StanceJudge failure is logged and swallowed rather than
    failing the write. Scans the existing Body of Knowledge for evidence bearing on
    the new anchor and raises Impacts where the judge sees genuine change.
    """
    try:
        impacts.detect_for_new_anchor(
            anchor_type,
            anchor_id,
            judge=ClaudeStanceJudge(config.anthropic_api_key(), config.stance_model()),
            repo=repo,
            candidate_limit=config.impact_candidate_limit(),
        )
    except Exception as exc:  # detection is a follow-on; never fail the write
        repo.rollback()
        logger.warning("reverse impact detection failed for %s %s: %s",
                       anchor_type, anchor_id, exc)


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


# == Personal layer: Goals, Markers, Decisions & linking (issue #16) ========
#
# The owner-specific layer (CONTEXT.md "Personal Layer"), recorded through guided
# forms and linked to the evidence layer by Concept overlap. Reads are plain
# repository reads; writes go through the `personal` service, which owns the
# transaction and normalizes Concept mentions through the same Embedder the admit
# pipeline uses (ADR-0008). A Marker reading is append-only — the database rejects
# any overwrite — and "out of range" is derived from the stored reference range,
# not a stored flag.


class NewGoal(BaseModel):
    """A Goal to record — a stable intention or risk and the Concepts it concerns."""

    title: str
    detail: str | None = None
    concepts: list[str] = []


class NewMarker(BaseModel):
    """A Marker reading to append: value + unit + reference range + date, against a
    Concept. Either reference bound may be omitted for a one-sided range."""

    concept: str
    value: float
    unit: str
    reference_low: float | None = None
    reference_high: float | None = None
    measured_at: datetime


class NewDecision(BaseModel):
    """A Decision to record, with its own actual parameters. `implements_protocol_id`
    is set by the "adopt a Protocol" path, which pre-fills the form and links the
    Protocol (issue #16)."""

    action: str
    dose: str | None = None
    timing: str | None = None
    frequency: str | None = None
    duration: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    note: str | None = None
    concepts: list[str] = []
    implements_protocol_id: int | None = None


class DecisionLink(BaseModel):
    """A link to confirm from a Decision to a Protocol/Goal/Marker/Claim."""

    target_type: str
    target_id: int


def _goal_dict(g: Goal) -> dict:
    return {
        "id": g.id,
        "title": g.title,
        "detail": g.detail,
        "concepts": [{"id": r.id, "name": r.name} for r in g.concepts],
        "served_by": [{"id": r.id, "action": r.action} for r in g.served_by],
    }


def _reading_dict(m: MarkerReading) -> dict:
    return {
        "id": m.id,
        "concept": {"id": m.concept.id, "name": m.concept.name},
        "value": m.value,
        "unit": m.unit,
        "reference_low": m.reference_low,
        "reference_high": m.reference_high,
        "measured_at": m.measured_at.isoformat(),
        "out_of_range": m.out_of_range,
    }


def _series_dict(s: MarkerSeries) -> dict:
    return {
        "concept": {"id": s.concept.id, "name": s.concept.name},
        "unit": s.unit,
        "reading_count": s.reading_count,
        "latest": _reading_dict(s.latest),
        "out_of_range": s.latest.out_of_range,
    }


def _decision_dict(d: Decision) -> dict:
    return {
        "id": d.id,
        "action": d.action,
        "dose": d.dose,
        "timing": d.timing,
        "frequency": d.frequency,
        "duration": d.duration,
        "started_at": d.started_at.isoformat(),
        "ended_at": d.ended_at.isoformat() if d.ended_at else None,
        "note": d.note,
        "concepts": [{"id": r.id, "name": r.name} for r in d.concepts],
        "implements": [{"id": r.id, "action": r.action} for r in d.implements],
        "serves": [{"id": r.id, "title": r.title} for r in d.serves],
        "motivated_by": [
            {
                "id": r.id,
                "concept": r.concept,
                "value": r.value,
                "unit": r.unit,
                "measured_at": r.measured_at.isoformat(),
            }
            for r in d.motivated_by
        ],
        "supported_by": [{"id": r.id, "text": r.text} for r in d.supported_by],
    }


def _suggestion_dict(s: SuggestedLink) -> dict:
    return {
        "target_type": s.target_type,
        "target_id": s.target_id,
        "label": s.label,
        "shared_concepts": s.shared_concepts,
    }


# -- Goals ------------------------------------------------------------------


@app.get("/api/goals")
def list_goals() -> dict:
    """Every Goal with the Concepts it concerns and the Decisions that serve it."""
    with _repo() as repo:
        goals = repo.list_goals()
    return {"goals": [_goal_dict(g) for g in goals]}


@app.post("/api/goals")
def create_goal(body: NewGoal) -> dict:
    """Record a Goal and its Concepts (normalized like a Claim's).

    A new Goal triggers the reverse Impact pass (issue #18): the existing Body of
    Knowledge is scanned for evidence bearing on it — an unmet Goal is the prime
    target for an `opportunity` Impact (CONTEXT.md "Goal").
    """
    with _repo() as repo:
        goal_id = personal.record_goal(
            title=body.title,
            detail=body.detail,
            concepts=body.concepts,
            normalizer=_normalizer(repo),
            repo=repo,
        )
        _detect_anchor_impacts(repo, "goal", goal_id)
    return {"id": goal_id}


@app.get("/api/goals/{goal_id}")
def goal_detail(goal_id: int) -> dict:
    """One Goal with its Concepts and serving Decisions (unmet if none serve it)."""
    with _repo() as repo:
        goal = repo.get_goal(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return _goal_dict(goal)


@app.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: int) -> dict:
    """Delete a Goal and the edges hanging off it (issue #16)."""
    with _repo() as repo:
        deleted = personal.delete_goal(goal_id, repo=repo)
    if not deleted:
        raise HTTPException(status_code=404, detail="goal not found")
    return {"id": goal_id, "deleted": True}


# -- Markers ----------------------------------------------------------------


@app.get("/api/markers")
def list_markers() -> dict:
    """One series per tracked Concept: its latest reading + derived out-of-range."""
    with _repo() as repo:
        series = repo.list_marker_series()
    return {"markers": [_series_dict(s) for s in series]}


@app.post("/api/markers")
def create_marker(body: NewMarker) -> dict:
    """Append a Marker reading — never an overwrite (CONTEXT.md "Marker")."""
    with _repo() as repo:
        reading_id = personal.record_marker(
            concept=body.concept,
            value=body.value,
            unit=body.unit,
            reference_low=body.reference_low,
            reference_high=body.reference_high,
            measured_at=body.measured_at,
            normalizer=_normalizer(repo),
            repo=repo,
        )
    return {"id": reading_id}


@app.get("/api/marker-readings")
def list_marker_readings() -> dict:
    """Every reading, newest first — the picker for a Decision's `motivated_by`."""
    with _repo() as repo:
        readings = repo.list_marker_readings()
    return {"readings": [_reading_dict(m) for m in readings]}


@app.get("/api/markers/{concept_id}")
def marker_history(concept_id: int) -> dict:
    """A Concept's whole Marker history as a series, each reading's out-of-range derived."""
    with _repo() as repo:
        readings = repo.marker_history(concept_id)
    return {"concept_id": concept_id, "readings": [_reading_dict(m) for m in readings]}


# -- Decisions --------------------------------------------------------------


@app.get("/api/decisions")
def list_decisions() -> dict:
    """Every Decision with the Concepts it references; connections come on detail."""
    with _repo() as repo:
        decisions = repo.list_decisions()
    return {"decisions": [_decision_dict(d) for d in decisions]}


@app.post("/api/decisions")
def create_decision(body: NewDecision) -> dict:
    """Record a Decision; an `implements_protocol_id` adopts that Protocol (issue #16).

    A new Decision triggers the reverse Impact pass (issue #18): the existing Body of
    Knowledge is scanned for evidence that reinforces, contradicts, or refines it.
    """
    with _repo() as repo:
        decision_id = personal.record_decision(
            action=body.action,
            dose=body.dose,
            timing=body.timing,
            frequency=body.frequency,
            duration=body.duration,
            started_at=body.started_at,
            ended_at=body.ended_at,
            note=body.note,
            concepts=body.concepts,
            implements_protocol_id=body.implements_protocol_id,
            normalizer=_normalizer(repo),
            repo=repo,
        )
        _detect_anchor_impacts(repo, "decision", decision_id)
    return {"id": decision_id}


@app.get("/api/decisions/{decision_id}")
def decision_detail(decision_id: int) -> dict:
    """One Decision with its full rationale: implements/serves/motivated_by/supported_by."""
    with _repo() as repo:
        decision = repo.get_decision(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return _decision_dict(decision)


@app.delete("/api/decisions/{decision_id}")
def delete_decision(decision_id: int) -> dict:
    """Delete a Decision and the edges hanging off it (issue #16)."""
    with _repo() as repo:
        deleted = personal.delete_decision(decision_id, repo=repo)
    if not deleted:
        raise HTTPException(status_code=404, detail="decision not found")
    return {"id": decision_id, "deleted": True}


@app.get("/api/decisions/{decision_id}/suggestions")
def decision_suggestions(decision_id: int) -> dict:
    """Protocols, Claims, and Goals relevant to a Decision by Concept overlap."""
    with _repo() as repo:
        suggestions = personal.suggest_decision_links(decision_id, repo=repo)
    return {"suggestions": [_suggestion_dict(s) for s in suggestions]}


@app.post("/api/decisions/{decision_id}/links")
def confirm_decision_link(decision_id: int, body: DecisionLink) -> dict:
    """Confirm a suggested (or owner-chosen) link from a Decision (issue #16)."""
    with _repo() as repo:
        try:
            linked = personal.link_decision(
                decision_id,
                target_type=body.target_type,
                target_id=body.target_id,
                repo=repo,
            )
        except UnknownLinkTarget as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not linked:
        raise HTTPException(status_code=404, detail="decision not found")
    return {"decision_id": decision_id, "linked": True}


@app.delete("/api/decisions/{decision_id}/links")
def detach_decision_link(
    decision_id: int, target_type: str, target_id: int
) -> dict:
    """Detach a previously-confirmed link from a Decision (issue #16)."""
    with _repo() as repo:
        try:
            removed = personal.unlink_decision(
                decision_id,
                target_type=target_type,
                target_id=target_id,
                repo=repo,
            )
        except UnknownLinkTarget as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="link not found")
    return {"decision_id": decision_id, "unlinked": True}


# == Natural-language query: grounded & cited (issue #17) ===================
#
# The primary way the owner *explores* the Body of Knowledge now that a visual
# graph is out of v1 scope (ADR-0009, ADR-0011). A free-text question is embedded
# (same Embedder as the admit pipeline), retrieval gathers the Claims/Protocols and
# personal-layer context sharing a Concept with it, and the QueryAnswerer (Claude)
# synthesizes an answer grounded only in that evidence — citing the specific Claims,
# each clickable through to its Source and locator, or abstaining honestly. The
# grounding and cite-or-abstain guarantees live in the `query` service, not here.


class Question(BaseModel):
    """The owner's free-text question for grounded query (ADR-0011)."""

    question: str


@app.post("/api/query")
def ask(body: Question) -> dict:
    """Answer a free-text question, grounded in and cited to the owner's library."""
    with _repo() as repo:
        answer = query.answer_question(
            body.question,
            embedder=OpenAIEmbedder(config.openai_api_key(), config.embedding_model()),
            answerer=ClaudeQueryAnswerer(
                config.anthropic_api_key(), config.query_model()
            ),
            repo=repo,
            model=config.embedding_model(),
            concept_limit=config.query_concept_limit(),
            max_distance=config.query_max_distance(),
            evidence_limit=config.query_evidence_limit(),
        )
    return {
        "question": body.question,
        "answer": answer.text,
        "abstained": answer.abstained,
        "citations": [
            {
                "claim_id": c.claim_id,
                "text": c.text,
                "type": c.type,
                "deep_link": c.deep_link,
                "source_title": c.source_title,
            }
            for c in answer.citations
        ],
    }


# == Impact engine: inbox & lifecycle (issue #18) ===========================
#
# Change detection's read/act surface. New Claims/Protocols (the worker, forward)
# and new Decisions/Goals (the create endpoints above, reverse) raise Impacts; here
# the owner reviews the inbox — filterable by stance and anchor — and walks each
# Impact `new → reviewed → actioned | dismissed` so it never re-nags. Actioning
# records the Decision the owner revised or created in response; a burst (e.g. after
# a backfill approval) can be bulk-dismissed in one gesture. The lifecycle and
# audit-trail guarantees live in the `impacts` service, not here.


class ActionImpact(BaseModel):
    """Actioning an Impact: the id of the Decision the owner revised or created."""

    decision_id: int


class BulkDismiss(BaseModel):
    """The Impact ids the owner is dismissing as a burst (issue #18)."""

    impact_ids: list[int]


def _impact_dict(i: Impact) -> dict:
    return {
        "id": i.id,
        "source": {"type": i.source_type, "id": i.source_id, "label": i.source_label},
        "anchor": {"type": i.anchor_type, "id": i.anchor_id, "label": i.anchor_label},
        "stance": i.stance,
        "state": i.state,
        "detail": i.detail,
        "actioned_decision_id": i.actioned_decision_id,
        "created_at": i.created_at.isoformat(),
    }


@app.get("/api/impacts")
def list_impacts(
    stance: str | None = None,
    anchor_type: str | None = None,
    anchor_id: int | None = None,
    state: str | None = None,
) -> dict:
    """The Impact inbox — unresolved findings, filterable by stance and anchor.

    With no `state` the unresolved (`new`/`reviewed`) Impacts show; an explicit
    `state` narrows to that lifecycle position (e.g. to review what was dismissed).
    """
    with _repo() as repo:
        found = impacts.inbox(
            stance=stance,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            state=state,
            repo=repo,
        )
    return {"impacts": [_impact_dict(i) for i in found]}


@app.post("/api/impacts/{impact_id}/review")
def review_impact(impact_id: int) -> dict:
    """Mark an Impact reviewed — the owner has seen it (issue #18)."""
    with _repo() as repo:
        ok = impacts.review_impact(impact_id, repo=repo)
    if not ok:
        raise HTTPException(status_code=404, detail="impact not found")
    return {"id": impact_id, "state": "reviewed"}


@app.post("/api/impacts/{impact_id}/dismiss")
def dismiss_impact(impact_id: int) -> dict:
    """Dismiss an Impact so it never re-nags (issue #18)."""
    with _repo() as repo:
        ok = impacts.dismiss_impact(impact_id, repo=repo)
    if not ok:
        raise HTTPException(status_code=404, detail="impact not found")
    return {"id": impact_id, "state": "dismissed"}


@app.post("/api/impacts/{impact_id}/action")
def action_impact(impact_id: int, body: ActionImpact) -> dict:
    """Action an Impact, recording the Decision it produced (issue #18).

    The owner revised or created a Decision in response; this records the link so the
    audit trail shows change detection driving change.
    """
    with _repo() as repo:
        ok = impacts.action_impact(impact_id, decision_id=body.decision_id, repo=repo)
    if not ok:
        raise HTTPException(status_code=404, detail="impact not found")
    return {"id": impact_id, "state": "actioned", "decision_id": body.decision_id}


@app.post("/api/impacts/dismiss")
def bulk_dismiss_impacts(body: BulkDismiss) -> dict:
    """Bulk-dismiss a burst of Impacts; returns how many were dismissed (issue #18)."""
    with _repo() as repo:
        dismissed = impacts.bulk_dismiss(body.impact_ids, repo=repo)
    return {"dismissed": dismissed}
