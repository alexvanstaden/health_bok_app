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
from datetime import datetime, time, timezone
from typing import Iterator

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import backfill, config, creators, curation, impacts, llm, personal, query, review
from .adapters.answerer import ChatQueryAnswerer
from .adapters.concept_proposer import ChatConceptProposer
from .adapters.embedder import OpenAIEmbedder
from .adapters.hierarchy_proposer import ChatHierarchyProposer
from .adapters.stance import ChatStanceJudge
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
    NearestConcept,
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


def _parse_date_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse a `YYYY-MM-DD` query bound into a UTC datetime for a publish-date
    filter (issue #76).

    The toolbar's date control sends calendar dates, so the range is inclusive on
    both ends: the `from` bound anchors at the start of its day and the `to` bound
    at the very end of its day. An absent or unparseable value means *no bound*, so
    a half-open or omitted range simply widens the query rather than erroring.
    """
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    moment = time.max if end_of_day else time.min
    return datetime.combine(day, moment, tzinfo=timezone.utc)


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

    Best-effort and synchronous (like `/api/query`'s LLM call): the anchor is
    already committed, so a StanceJudge failure is logged and swallowed rather than
    failing the write. Scans the existing Body of Knowledge for evidence bearing on
    the new anchor and raises Impacts where the judge sees genuine change.
    """
    # Scope-widening first (ADR-0013): one summary Impact for the relationships
    # already sitting under each Concept the new anchor tracks, so widening interest
    # never detonates a burst. Structural and deterministic — no LLM.
    try:
        impacts.detect_scope_widening(anchor_type, anchor_id, repo=repo)
    except Exception as exc:
        repo.rollback()
        logger.warning("scope-widening detection failed for %s %s: %s",
                       anchor_type, anchor_id, exc)
    try:
        impacts.detect_for_new_anchor(
            anchor_type,
            anchor_id,
            judge=ChatStanceJudge(llm.chat_model(config.stance_model())),
            repo=repo,
            candidate_limit=config.impact_candidate_limit(),
        )
    except Exception as exc:  # detection is a follow-on; never fail the write
        repo.rollback()
        logger.warning("reverse impact detection failed for %s %s: %s",
                       anchor_type, anchor_id, exc)


def _detect_broader_of_scope_widening(
    repo: Repository, broader_id: int, narrower_id: int
) -> None:
    """Raise the scope-widening summary a just-confirmed `broader-of` edge owes.

    Best-effort, like the anchor pass: the edge is already committed, so a detection
    hiccup is logged and swallowed rather than failing the confirm. Structural and
    deterministic — no LLM (ADR-0013)."""
    try:
        impacts.detect_scope_widening_for_broader_of(
            broader_id, narrower_id, repo=repo
        )
    except Exception as exc:  # detection is a follow-on; never fail the write
        repo.rollback()
        logger.warning("scope-widening detection failed for broader-of %s>%s: %s",
                       broader_id, narrower_id, exc)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/candidates")
def list_candidates(
    status: list[str] | None = Query(default=None),
    creator: list[str] | None = Query(default=None),
    published_from: str | None = Query(default=None),
    published_to: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> dict:
    """The daily review queue: Candidates with their Summary and state (ADR-0007).

    `status` is an optional, repeatable processing-status filter (issue #75) — one
    or more of `candidate`/`approved`/`processing`/`failed`. Omitting it returns the
    full queue; unknown values are ignored server-side.

    `creator` (repeatable channel_ids), `published_from`/`published_to` (`YYYY-MM-DD`
    bounds on the publish date), and `q` (free-text over title + creator name + the
    Summary body) narrow further (issue #76). Each is optional and they compose with
    `status` and each other via AND, so the queue is their intersection.
    """
    with _repo() as repo:
        candidates = repo.list_daily_candidates(
            statuses=status,
            creators=creator,
            published_from=_parse_date_bound(published_from, end_of_day=False),
            published_to=_parse_date_bound(published_to, end_of_day=True),
            search=q,
        )
    return {
        "candidates": [
            {
                "video_id": c.video_id,
                "title": c.title,
                "url": c.url,
                "summary": c.summary,
                "state": c.state,
                "published_at": c.published_at.isoformat(),
                "creator": c.creator,
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


class BulkApprove(BaseModel):
    """The backfill Candidate video_ids the owner is approving in one gesture."""

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


def _backfill_payload(c) -> dict:
    """One backfill Candidate as the Web App's JSON — shared by the queue and the
    lazy detail fetch so both return the identical shape (issue #31)."""
    return {
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


@app.get("/api/backfill")
def list_backfill(
    order: str = "newest",
    status: list[str] | None = Query(default=None),
    creator: list[str] | None = Query(default=None),
    published_from: str | None = Query(default=None),
    published_to: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> dict:
    """The backfill review queue: metadata-only Candidates awaiting a decision.

    `order` sorts by publish date — `newest` (default) or `oldest` (issue #31).
    `status` is an optional, repeatable processing-status filter (issue #75) — one
    or more of `candidate`/`approved`/`processing`/`failed`; omitting it returns the
    full queue.

    `creator` (repeatable channel_ids), `published_from`/`published_to` (`YYYY-MM-DD`
    bounds on the publish date), and `q` (free-text over title + creator name +
    description) narrow further (issue #76). All filters compose with the status
    filter and each other via AND; the narrowed set is then ordered.
    """
    with _repo() as repo:
        candidates = repo.list_backfill_candidates(
            newest_first=order != "oldest",
            statuses=status,
            creators=creator,
            published_from=_parse_date_bound(published_from, end_of_day=False),
            published_to=_parse_date_bound(published_to, end_of_day=True),
            search=q,
        )
    return {"candidates": [_backfill_payload(c) for c in candidates]}


@app.post("/api/backfill/{video_id}/fetch-details")
def fetch_backfill_details(video_id: str) -> dict:
    """Lazily fetch one Candidate's real description + accurate publish date (issue #31).

    Performs a single per-video YouTube extraction, persists both fields on the
    Candidate, and returns the updated Candidate so the queue shows them in place.
    Idempotent — safe to re-run. 404 if the video_id names no backfill Candidate.
    """
    with _repo() as repo:
        candidate = backfill.fetch_candidate_details(
            video_id, content_source=YouTubeContentSource(), repo=repo
        )
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return _backfill_payload(candidate)


@app.post("/api/backfill/reject")
def reject_backfill(body: BulkReject) -> dict:
    """Bulk-reject obvious backfill noise; returns how many were rejected."""
    with _repo() as repo:
        rejected = review.bulk_reject(body.video_ids, repo=repo)
    return {"rejected": rejected}


@app.post("/api/backfill/approve")
def approve_backfill(body: BulkApprove) -> dict:
    """Bulk-approve the selected backfill Candidates (issue #73).

    Approves every selected Candidate in one gesture and returns how many were
    *newly* approved — already in-flight ones are skipped, so it is safe to re-send.
    """
    with _repo() as repo:
        approved = review.bulk_approve(body.video_ids, repo=repo)
    return {"approved": approved}


def _snippet(body: str, *, limit: int = 280) -> str:
    """A brief description for the Logs list: the Summary's opening, trimmed at a
    word boundary so a row stays glanceable (issue #33). The full Summary lives on
    the video's own pages; here it is only a recognisable hint."""
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + "…"


@app.get("/api/videos")
def list_videos() -> dict:
    """The Logs page: a read-only record of admitted/failed video Sources (issue #33).

    Newest-added first, each with its title, Creator, the date it was added, a
    snippet of its latest Summary (`null` when the video was admitted without one —
    e.g. a backfill admission, issue #79), and a BoK-state badge (admitted / failed).
    Only videos that reached a terminal admission are listed — ones still in flight or
    never approved are hidden. Backed by one repository query; the page links each
    row to the video's Claims page. Read-only: no actions.
    """
    with _repo() as repo:
        videos = repo.list_processed_videos()
    return {
        "videos": [
            {
                "video_id": v.video_id,
                "title": v.title,
                "creator": v.creator_name,
                "added_at": v.added_at.isoformat(),
                "summary": _snippet(v.summary) if v.summary else None,
                "bok_state": v.bok_state,
            }
            for v in videos
        ]
    }


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
    """Delete a Claim and the edges hanging off it (issue #14).

    Self-healing made audible (ADR-0013): if the Claim was the last evidence behind
    a lateral relationship a Decision relied on, an `eroded` Impact is raised so the
    vanishing basis is surfaced rather than silently dropped.
    """
    with _repo() as repo:
        deleted, _ = impacts.delete_claim_with_alerts(claim_id, repo=repo)
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


# == Concept↔Concept relationships & the broader-of taxonomy (ADR-0013) ======
#
# Lateral relationships are a materialized projection of Claims (derived at admit
# time, self-healing). The neighbourhood view rolls a Concept's whole subtree up,
# ranked by Strength and flagged when contested. Hierarchy is the one curated link:
# the system proposes broader parents (LLM over the embedding cluster) and the owner
# confirms — a proposal stays invisible to roll-up until then.


class NewBroaderOf(BaseModel):
    """A proposed `broader-of` edge: the broader Concept this one rolls up under."""

    broader_id: int


class TrustTier(BaseModel):
    """An owner-set trust-tier on a Creator (>=1), weighting it in Strength."""

    tier: int


@app.get("/api/concepts/{concept_id}/neighbourhood")
def concept_neighbourhood(concept_id: int) -> dict:
    """A Concept's roll-up neighbourhood: sub-Concepts + subtree relationships (ADR-0013).

    Every lateral relationship in the Concept's confirmed `broader-of` subtree,
    attributed to the descendant it lives on, deduped across DAG diamonds, ranked by
    evidence Strength, and flagged when the pair is contested. Each relationship
    carries its evidencing Claims as Citations (Source + locator deep-link) — the
    same shape NL Query cites (ADR-0011) — so the owner can click straight through to
    the moment each connection was asserted (issue #51).
    """
    with _repo() as repo:
        hood = repo.concept_neighbourhood(concept_id)
    if hood is None:
        raise HTTPException(status_code=404, detail="concept not found")
    return _neighbourhood_dict(hood)


@app.get("/api/broader-of/proposals")
def broader_of_proposals() -> dict:
    """The review queue: every unconfirmed `broader-of` proposal (ADR-0014).

    The two-tier auto path (`hierarchy auto` and the post-admission worker step)
    confirms confident parents outright and leaves looser ones *proposed* — this is
    where those land for one-click confirm/reject, each carrying the narrower and
    broader Concepts' ids + names so the Web App renders and acts on them directly.
    """
    with _repo() as repo:
        return {"proposals": repo.broader_of_proposals()}


@app.get("/api/concepts/{concept_id}/broader-of/suggestions")
def suggest_broader_of(concept_id: int) -> dict:
    """Broader Concepts this one could roll up under, for one-click confirm (ADR-0013)."""
    model = config.embedding_model()
    with _repo() as repo:
        suggestions = curation.suggest_broader_of(
            concept_id,
            proposer=ChatHierarchyProposer(
                llm.chat_model(config.hierarchy_proposal_model())
            ),
            embedder=OpenAIEmbedder(config.openai_api_key(), model),
            repo=repo,
            model=model,
        )
    return {"suggestions": [{"id": c.id, "name": c.name} for c in suggestions]}


@app.post("/api/concepts/{narrower_id}/broader-of")
def propose_broader_of(narrower_id: int, body: NewBroaderOf) -> dict:
    """Propose a `broader-of` edge — a suggestion, invisible to roll-up until confirmed."""
    with _repo() as repo:
        try:
            ok = curation.propose_broader_of(body.broader_id, narrower_id, repo=repo)
        except psycopg.errors.RaiseException as exc:  # cycle guard
            raise HTTPException(status_code=409, detail=str(exc)) from None
    if not ok:
        raise HTTPException(status_code=404, detail="concept not found")
    return {"broader_id": body.broader_id, "narrower_id": narrower_id, "confirmed": False}


@app.post("/api/concepts/{narrower_id}/broader-of/{broader_id}/confirm")
def confirm_broader_of(narrower_id: int, broader_id: int) -> dict:
    """Confirm a proposed `broader-of` edge, making it visible to roll-up (ADR-0013)."""
    with _repo() as repo:
        ok = curation.confirm_broader_of(broader_id, narrower_id, repo=repo)
        if ok:
            _detect_broader_of_scope_widening(repo, broader_id, narrower_id)
    if not ok:
        raise HTTPException(status_code=404, detail="no such proposed edge")
    return {"broader_id": broader_id, "narrower_id": narrower_id, "confirmed": True}


@app.delete("/api/concepts/{narrower_id}/broader-of/{broader_id}")
def reject_broader_of(narrower_id: int, broader_id: int) -> dict:
    """Reject (delete) a proposed-or-confirmed `broader-of` edge (ADR-0013)."""
    with _repo() as repo:
        ok = curation.reject_broader_of(broader_id, narrower_id, repo=repo)
    if not ok:
        raise HTTPException(status_code=404, detail="no such edge")
    return {"broader_id": broader_id, "narrower_id": narrower_id, "deleted": True}


@app.put("/api/creators/{creator_id}/trust-tier")
def set_creator_trust_tier(creator_id: int, body: TrustTier) -> dict:
    """Set the owner's trust-tier on a Creator (ADR-0013 "Strength")."""
    if body.tier < 1:
        raise HTTPException(status_code=422, detail="trust tier must be >= 1")
    with _repo() as repo:
        ok = repo.set_creator_trust_tier(creator_id, body.tier)
        if ok:
            repo.commit()
    if not ok:
        raise HTTPException(status_code=404, detail="creator not found")
    return {"creator_id": creator_id, "trust_tier": body.tier}


def _neighbour_relation_dict(r) -> dict:
    return {
        "relation_id": r.relation_id,
        "src": {"id": r.src_concept_id, "name": r.src_name},
        "predicate": r.predicate,
        "dst": {"id": r.dst_concept_id, "name": r.dst_name},
        "strength": round(r.strength, 4),
        "creator_count": r.creator_count,
        "contested": r.contested,
        "via": (
            {"id": r.via_concept_id, "name": r.via_concept_name}
            if r.via_concept_id is not None else None
        ),
        "evidence_claim_ids": r.evidence_claim_ids,
        # The evidencing Claims, clickable through to Source + locator — the same
        # Citation shape NL Query returns (ADR-0011), so both surfaces agree.
        "evidence": [
            {
                "claim_id": c.claim_id,
                "text": c.text,
                "type": c.type,
                "deep_link": c.deep_link,
                "source_title": c.source_title,
            }
            for c in r.evidence
        ],
    }


def _neighbourhood_dict(hood) -> dict:
    return {
        "concept": {"id": hood.concept_id, "name": hood.concept_name},
        "sub_concepts": [{"id": c.id, "name": c.name} for c in hood.sub_concepts],
        "relations": [_neighbour_relation_dict(r) for r in hood.relations],
    }


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


class GoalConcept(BaseModel):
    """A Concept to attach to a Goal — picked from the catalogue or typed new (issue
    #37). Either way the term is normalized like a Claim's."""

    name: str


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


def _concept_suggestion_dict(c: NearestConcept) -> dict:
    return {"concept_id": c.concept_id, "name": c.name, "distance": c.distance}


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


@app.post("/api/goals/{goal_id}/concepts")
def attach_goal_concept(goal_id: int, body: GoalConcept) -> dict:
    """Attach a Concept to a Goal, normalized like a Claim's (issue #37).

    The owner picks from the existing catalogue or types a new term; the
    `ConceptNormalizer` reuses an existing Concept or mints one, so no near-duplicate
    hub is created. Idempotent — re-adding an attached Concept is a no-op.
    """
    with _repo() as repo:
        try:
            attached = personal.attach_goal_concept(
                goal_id, name=body.name, normalizer=_normalizer(repo), repo=repo
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not attached:
        raise HTTPException(status_code=404, detail="goal not found")
    return {"goal_id": goal_id, "attached": True}


@app.delete("/api/goals/{goal_id}/concepts/{concept_id}")
def detach_goal_concept(goal_id: int, concept_id: int) -> dict:
    """Detach a Concept from a Goal (issue #37); an empty Concept set is allowed."""
    with _repo() as repo:
        removed = personal.detach_goal_concept(
            goal_id, concept_id=concept_id, repo=repo
        )
    if not removed:
        raise HTTPException(status_code=404, detail="concept not attached to goal")
    return {"goal_id": goal_id, "detached": True}


@app.get("/api/goals/{goal_id}/concept-suggestions")
def goal_concept_suggestions(goal_id: int) -> dict:
    """Existing Concepts a Goal likely concerns, inferred from its title + detail
    (issue #38).

    Embedding-driven and conservative (ADR-0008): the Goal's text is matched against
    the existing Concept embeddings over pgvector, so every suggestion is a Concept
    that already exists — none minted, the LLM untouched — and Concepts already
    attached are excluded. Each is confirmable in one click through the attach
    endpoint (#37). A Goal whose text matches nothing yields an empty list.
    """
    model = config.embedding_model()
    with _repo() as repo:
        if repo.get_goal(goal_id) is None:
            raise HTTPException(status_code=404, detail="goal not found")
        suggestions = personal.suggest_goal_concepts(
            goal_id,
            embedder=OpenAIEmbedder(config.openai_api_key(), model),
            repo=repo,
            model=model,
        )
    return {"suggestions": [_concept_suggestion_dict(s) for s in suggestions]}


@app.get("/api/goals/{goal_id}/new-concept-suggestions")
def goal_new_concept_suggestions(goal_id: int) -> dict:
    """New (not-yet-existing) Concepts to mint for a Goal, inferred from its title +
    detail (issue #39).

    The companion to `/concept-suggestions`: an LLM proposes candidate Concept terms
    the Goal concerns, and each is checked against the existing catalogue with the
    same conservative `ConceptNormalizer` logic — a term that resolves to an existing
    Concept is dropped (no near-duplicate hub), and only a genuinely new term is
    returned. Read-only: nothing is minted here. Confirming one is an attach through
    `/concepts` (#37), which mints the Concept and attaches it in one step, so minting
    stays owner-confirmed (ADR-0004). A model failure degrades to an empty list — the
    existing-Concept suggestions (a separate call) keep working.
    """
    model = config.embedding_model()
    with _repo() as repo:
        if repo.get_goal(goal_id) is None:
            raise HTTPException(status_code=404, detail="goal not found")
        terms = personal.suggest_new_goal_concepts(
            goal_id,
            proposer=ChatConceptProposer(llm.chat_model(config.concept_proposal_model())),
            normalizer=_normalizer(repo),
            repo=repo,
        )
    return {"suggestions": [{"name": t} for t in terms]}


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
# personal-layer context sharing a Concept with it, and the QueryAnswerer (the LLM)
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
            answerer=ChatQueryAnswerer(llm.chat_model(config.query_model())),
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
        "tier": i.tier,
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


@app.get("/api/impacts/feed")
def impacts_feed(stance: str | None = None) -> dict:
    """The quieter Tier-2 browsable feed of notable structural changes (ADR-0013).

    Field-wide relationship developments that touch no tracked anchor but cleared the
    Strength threshold — available on demand, deliberately not in the push inbox.
    """
    with _repo() as repo:
        found = impacts.tier2_feed(stance=stance, repo=repo)
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
