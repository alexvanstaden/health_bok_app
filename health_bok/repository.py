"""Persistence against the single source-of-truth Postgres (ADR-0003).

The store is deliberately *not* a port: integration tests run it for real
(PRD #1). All writes for one video commit together so a crash never leaves a
half-archived video, keeping the job idempotent and crash-safe (user story 22).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import psycopg

from .predicates import contradicts, tensions
from .strength import (
    DEFAULT_HALF_LIFE_DAYS,
    EvidenceContribution,
    distinct_creator_count,
    relation_strength,
)
from .models import (
    CandidateMetadata,
    Citation,
    CreatorIdentity,
    EvidenceClaim,
    EvidenceDecision,
    EvidenceGoal,
    EvidenceMarker,
    EvidenceProtocol,
    FetchedTranscript,
    ImpactAnchor,
    ImpactKnowledge,
    Provenance,
    RetrievedEvidence,
    TranscriptSegment,
    locator_url,
    thumbnail_url,
)

# The implicit lifecycle state of a daily Candidate that has no `admissions` row
# yet: a plain, un-acted-on candidate (CONTEXT.md "Candidate"; ADR-0004).
CANDIDATE = "candidate"


@dataclass(frozen=True)
class ArchivedSummary:
    """A persisted Summary, read back for assembling the Digest."""

    video_id: str
    title: str
    url: str
    body: str


@dataclass(frozen=True)
class DailyCandidate:
    """A daily Candidate for the Web App's review queue (CONTEXT.md, ADR-0007).

    A video already processed (Transcript + Summary) but not yet admitted, shown
    with its Summary and current lifecycle `state` so the owner can approve,
    reject, or — when extraction failed — retry it.
    """

    video_id: str
    title: str
    url: str
    summary: str
    state: str
    published_at: datetime


@dataclass(frozen=True)
class ProcessedVideo:
    """A processed video Source for the Logs page (issue #33).

    A read-only record row: a video the pipeline has fully processed (Transcript +
    Summary) that has reached a terminal admission. `bok_state` is `admitted` (it
    reached the Body of Knowledge) or `failed` (extraction errored). Videos still in
    flight or never acted on are not listed — the log shows only admitted or failed.
    The Logs page is labelled "Logs" by the owner's explicit choice — a known
    divergence from the CONTEXT.md "Source" glossary.
    """

    video_id: str
    title: str
    creator_name: str
    added_at: datetime
    summary: str
    bok_state: str  # 'admitted' | 'failed'


@dataclass(frozen=True)
class QueuedJob:
    """A claimed unit of background work drained by the worker (ADR-0009)."""

    id: int
    kind: str
    video_id: str
    attempts: int


@dataclass(frozen=True)
class NearestConcept:
    """The closest existing Concept to a proposed mention, by cosine distance."""

    concept_id: int
    name: str
    distance: float


@dataclass(frozen=True)
class AdmittedClaim:
    """A persisted Claim read back for the Web App, with its locator deep-link."""

    id: int
    text: str
    type: str
    locator_seconds: int
    deep_link: str
    concepts: list[str]


@dataclass(frozen=True)
class AdmittedProtocol:
    """A persisted Protocol read back for the Web App, with its locator deep-link."""

    id: int
    action: str
    dose: str | None
    timing: str | None
    frequency: str | None
    duration: str | None
    locator_seconds: int
    deep_link: str
    concepts: list[str]


# -- Body-of-Knowledge browser shapes (issue #14) ---------------------------
#
# The browsable, editable evidence layer (ADR-0009 "no visual graph"): list and
# detail reads over the typed entity tables, with connections resolved by
# traversing `edges` (ADR-0008). A detail view carries the *other* end of each
# connection as a lightweight ref the Web App turns into a navigable link, so the
# owner follows Claim → Protocol → Concept by clicking, not by reading a graph.


@dataclass(frozen=True)
class ConceptRef:
    """A Concept as the far end of a connection: enough to label and link to it."""

    id: int
    name: str


@dataclass(frozen=True)
class ClaimRef:
    """A Claim as the far end of a connection (its text labels the link)."""

    id: int
    text: str


@dataclass(frozen=True)
class ProtocolRef:
    """A Protocol as the far end of a connection (its action labels the link)."""

    id: int
    action: str


@dataclass(frozen=True)
class BokClaim:
    """A Claim in the BoK browser: its text, sub-kind, provenance + locator
    deep-link, the `protected` flag, and the Concepts it references. A *detail*
    read additionally fills `supports` — the Protocols this Claim justifies
    (ADR-0008 `claim → protocol supports`); list reads leave it empty.
    """

    id: int
    text: str
    type: str
    locator_seconds: int
    deep_link: str
    protected: bool
    source_video_id: str
    source_title: str
    concepts: list[ConceptRef]
    supports: list[ProtocolRef] = field(default_factory=list)


@dataclass(frozen=True)
class BokProtocol:
    """A Protocol in the BoK browser: its structured parameters, provenance +
    locator deep-link, the `protected` flag, and referenced Concepts. A *detail*
    read fills `justified_by` — the Claims that support it; list reads leave it
    empty.
    """

    id: int
    action: str
    dose: str | None
    timing: str | None
    frequency: str | None
    duration: str | None
    locator_seconds: int
    deep_link: str
    protected: bool
    source_video_id: str
    source_title: str
    concepts: list[ConceptRef]
    justified_by: list[ClaimRef] = field(default_factory=list)


@dataclass(frozen=True)
class BokConcept:
    """A Concept hub node in the BoK browser. List reads carry only
    `reference_count` (how many Claims + Protocols point at it); a *detail* read
    fills `claims` and `protocols` — everything that references it (ADR-0008).
    """

    id: int
    name: str
    kind: str | None
    reference_count: int
    claims: list[ClaimRef] = field(default_factory=list)
    protocols: list[ProtocolRef] = field(default_factory=list)


@dataclass(frozen=True)
class ConceptRelation:
    """A lateral Concept→Concept relationship and the Claims that evidence it (ADR-0013).

    The materialized projection of the owner's Claims: a directed
    `src --predicate--> dst` link whose truth comes only from `evidence_claim_ids`
    (ADR-0011). Lose the last evidencing Claim and the relationship is removed — it
    never asserts something no Claim beneath it says.
    """

    id: int
    src_concept_id: int
    src_name: str
    predicate: str
    dst_concept_id: int
    dst_name: str
    evidence_claim_ids: list[int]


@dataclass(frozen=True)
class ContestedPair:
    """Whether a directed Concept pair is contested, and which predicates clash (ADR-0013).

    The contradiction verdict for one ordered (src, dst) pair: every distinct
    `predicate` the owner's Claims assert between them, and the `tensions` among
    those — the predicate pairs that disagree (an opposite signed pair, or
    `no_effect_on` against any signed predicate). Contradiction is *derived*, never
    merged (ADR-0002): both predicates stand as evidenced relationships and the pair
    is simply *flagged* contested, so the disagreement stays visible (user story 13).
    """

    src_concept_id: int
    src_name: str
    dst_concept_id: int
    dst_name: str
    predicates: list[str]
    tensions: list[tuple[str, str]]

    @property
    def contested(self) -> bool:
        """True when at least one predicate pair on this ordered pair is in tension."""
        return bool(self.tensions)


@dataclass(frozen=True)
class NeighbourRelation:
    """One lateral relationship in a Concept's neighbourhood, ranked & attributed (ADR-0013).

    Carries the directed link, its evidence Strength and distinct-creator count
    (what ranks it), whether the pair is `contested` (an opposite or `no_effect_on`
    predicate also holds on the same ordered pair), the `evidence` Claims behind it
    — each a `Citation` clickable through to its Source + locator, the *same* shape
    natural-language Query cites (ADR-0011), so the two surfaces show one consistent
    picture — and — once hierarchy roll-up lands (slice 3) — `via_*`, the descendant
    Concept the relationship actually lives on when surfaced at a broader ancestor
    ("via Brain metabolism").
    """

    relation_id: int
    src_concept_id: int
    src_name: str
    predicate: str
    dst_concept_id: int
    dst_name: str
    strength: float
    creator_count: int
    contested: bool
    evidence_claim_ids: list[int]
    evidence: list[Citation] = field(default_factory=list)
    via_concept_id: int | None = None
    via_concept_name: str | None = None


@dataclass(frozen=True)
class Neighbourhood:
    """A Concept's neighbourhood: its sub-Concepts and every relationship around it.

    The roll-up view (ADR-0013): the selected Concept, its sub-Concepts (the
    `broader-of` children, slice 3), and the lateral relationships in its subtree —
    deduped across DAG diamonds, attributed to the descendant they came from, and
    ranked by Strength so the best-supported connections surface first.
    """

    concept_id: int
    concept_name: str
    sub_concepts: list[ConceptRef]
    relations: list[NeighbourRelation]


def _attribution(
    anchor_id: int,
    subtree: set[int],
    src_id: int,
    src_name: str,
    dst_id: int,
    dst_name: str,
) -> dict:
    """Where a relationship surfaced at the anchor actually lives (ADR-0013).

    A relationship touching the anchor directly is shown unattributed (`via` None);
    one that only touches a *descendant* (rolled up under a broader ancestor) is
    attributed to that descendant ("via Brain metabolism"), so the owner knows
    where the connection lives. Prefers the `src` endpoint when both ends are
    descendants.
    """
    if src_id == anchor_id or dst_id == anchor_id:
        return {"via_concept_id": None, "via_concept_name": None}
    if src_id in subtree:
        return {"via_concept_id": src_id, "via_concept_name": src_name}
    return {"via_concept_id": dst_id, "via_concept_name": dst_name}


def _vector_literal(embedding: list[float]) -> str:
    """Render a Python vector as a pgvector text literal (cast `::vector` in SQL).

    psycopg has no native pgvector type, so embeddings cross the boundary as the
    `[0.1,0.2,…]` literal pgvector parses; keeping this in one place stops the
    format leaking into the queries.
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _concept_refs_sql(src_type: str, src_id_expr: str) -> str:
    """A scalar subquery yielding a row's referenced Concepts as a JSON array.

    The `references` edges from one Claim/Protocol to its Concepts, rolled up into
    `[{"id":…,"name":…}]` so a list read needs no N+1 follow-ups. `src_type` and
    `src_id_expr` are fixed literals the caller controls (`'claim'`/`'protocol'`,
    `cl.id`/`p.id`), never user input — no injection surface.
    """
    return (
        "COALESCE((SELECT json_agg("
        "json_build_object('id', c.id, 'name', c.name) ORDER BY c.name) "
        "FROM edges e JOIN concepts c ON c.id = e.dst_id "
        f"WHERE e.src_type = '{src_type}' AND e.src_id = {src_id_expr} "
        "AND e.dst_type = 'concept' AND e.kind = 'references'), '[]'::json)"
    )


# The shared projections for Claim/Protocol browse reads: every column the
# `BokClaim`/`BokProtocol` shapes need, including provenance (the Source's URL +
# title) and the rolled-up referenced Concepts. List and detail reads bolt their
# own WHERE/ORDER onto these so the column order the mappers below depend on stays
# in one place.
_CLAIM_SELECT = (
    "SELECT cl.id, cl.text, cl.type, cl.locator_seconds, v.url, v.video_id, "
    "v.title, cl.protected, " + _concept_refs_sql("claim", "cl.id") + " "
    "FROM claims cl JOIN videos v ON v.video_id = cl.video_id"
)
_PROTOCOL_SELECT = (
    "SELECT p.id, p.action, p.dose, p.timing, p.frequency, p.duration, "
    "p.locator_seconds, v.url, v.video_id, v.title, p.protected, "
    + _concept_refs_sql("protocol", "p.id") + " "
    "FROM protocols p JOIN videos v ON v.video_id = p.video_id"
)


def _row_to_bok_claim(r) -> BokClaim:
    return BokClaim(
        id=r[0],
        text=r[1],
        type=r[2],
        locator_seconds=r[3],
        deep_link=locator_url(r[4], r[3]),
        source_video_id=r[5],
        source_title=r[6],
        protected=r[7],
        concepts=[ConceptRef(id=c["id"], name=c["name"]) for c in r[8]],
    )


def _row_to_bok_protocol(r) -> BokProtocol:
    return BokProtocol(
        id=r[0],
        action=r[1],
        dose=r[2],
        timing=r[3],
        frequency=r[4],
        duration=r[5],
        locator_seconds=r[6],
        deep_link=locator_url(r[7], r[6]),
        source_video_id=r[8],
        source_title=r[9],
        protected=r[10],
        concepts=[ConceptRef(id=c["id"], name=c["name"]) for c in r[11]],
    )


@dataclass(frozen=True)
class StoredCandidate:
    """A persisted backfill Candidate, read back for the approval queue / tests.

    Metadata only — no Transcript or Summary — carrying the Creator's stable
    `channel_id` (and `channel_name`, for the Web App's backfill queue) so a
    Candidate stays attributable to whom it was backfilled for. `state` is the
    Candidate's lifecycle state for the queue read — `candidate` until the owner
    acts; `list_candidates` leaves it at that default.
    """

    video_id: str
    channel_id: str
    title: str
    description: str
    url: str
    published_at: datetime
    channel_name: str = ""
    state: str = CANDIDATE

    @property
    def thumbnail_url(self) -> str:
        """Thumbnail image URL, so the owner judges a backfill Candidate at a glance."""
        return thumbnail_url(self.video_id)


# -- Personal-layer browser shapes (issue #16) ------------------------------
#
# The owner-specific layer (CONTEXT.md "Personal Layer"): Goals, Markers, and
# Decisions, read back for the Web App. Like the BoK browser shapes a detail read
# carries the *other* end of each connection as a lightweight ref the Web App turns
# into a navigable link; a reading's "out of range" is *derived* here from the
# stored reference range (CONTEXT.md "Marker"), never a persisted flag.


@dataclass(frozen=True)
class GoalRef:
    """A Goal as the far end of a connection (its title labels the link)."""

    id: int
    title: str


@dataclass(frozen=True)
class DecisionRef:
    """A Decision as the far end of a connection (its action labels the link)."""

    id: int
    action: str


@dataclass(frozen=True)
class MarkerRef:
    """A Marker reading as the far end of a `motivated_by` link — enough to label it."""

    id: int
    concept: str
    value: float
    unit: str
    measured_at: datetime


@dataclass(frozen=True)
class Goal:
    """A Goal in the personal-layer browser: its title/detail, the Concepts it
    concerns, and the Decisions that serve it. An *unmet* Goal has an empty
    `served_by` — the prime target for an `opportunity` Impact later (CONTEXT.md).
    """

    id: int
    title: str
    detail: str | None
    concepts: list[ConceptRef]
    served_by: list[DecisionRef] = field(default_factory=list)


@dataclass(frozen=True)
class MarkerReading:
    """One dated Marker reading referencing a Concept. `out_of_range` is *derived*
    from the stored reference range — below `reference_low` or above
    `reference_high` (either bound may be absent for a one-sided range) — never a
    stored flag (CONTEXT.md "Marker").
    """

    id: int
    concept: ConceptRef
    value: float
    unit: str
    reference_low: float | None
    reference_high: float | None
    measured_at: datetime

    @property
    def out_of_range(self) -> bool:
        if self.reference_low is not None and self.value < self.reference_low:
            return True
        if self.reference_high is not None and self.value > self.reference_high:
            return True
        return False


@dataclass(frozen=True)
class MarkerSeries:
    """A Marker as a time-series, one per referenced Concept: its latest reading
    (carrying the derived out-of-range) and how many readings the history holds.
    """

    concept: ConceptRef
    unit: str
    reading_count: int
    latest: MarkerReading


@dataclass(frozen=True)
class Decision:
    """A Decision in the personal-layer browser. It holds its *own actual*
    parameters — distinct from the Protocol it implements, so deviation is
    first-class (CONTEXT.md "Decision"). A *detail* read fills every connection
    that forms its rationale: the Protocol(s) it `implements`, the Goal(s) it
    `serves`, the Marker(s) that `motivated_by` it, the Claim(s) that `support` it,
    and the Concepts it references. A list read fills only `concepts`.
    """

    id: int
    action: str
    dose: str | None
    timing: str | None
    frequency: str | None
    duration: str | None
    started_at: datetime
    ended_at: datetime | None
    note: str | None
    concepts: list[ConceptRef]
    implements: list[ProtocolRef] = field(default_factory=list)
    serves: list[GoalRef] = field(default_factory=list)
    motivated_by: list[MarkerRef] = field(default_factory=list)
    supported_by: list[ClaimRef] = field(default_factory=list)


@dataclass(frozen=True)
class SuggestedLink:
    """A suggest-then-confirm candidate for a Decision (issue #16): an entity that
    shares a Concept with the Decision and so may be worth linking. `target_type`
    is 'protocol' | 'claim' | 'goal'; confirming asserts the matching edge
    (implements | supports | serves). `shared_concepts` are the overlapping Concept
    names, so the owner can see *why* it was suggested before confirming.
    """

    target_type: str
    target_id: int
    label: str
    shared_concepts: list[str]


# Shared projections for the personal-layer reads, mirroring the BoK ones above:
# every column a `Goal`/`Decision`/`MarkerReading` shape needs, with referenced
# Concepts (and, for a Goal, its serving Decisions) rolled up so a list read needs
# no N+1 follow-ups. List and detail reads bolt their own WHERE onto these.
_GOAL_SELECT = (
    "SELECT g.id, g.title, g.detail, " + _concept_refs_sql("goal", "g.id") + ", "
    "COALESCE((SELECT json_agg(json_build_object('id', d.id, 'action', d.action) "
    "ORDER BY d.action, d.id) FROM edges e JOIN decisions d ON d.id = e.src_id "
    "WHERE e.dst_type = 'goal' AND e.dst_id = g.id AND e.src_type = 'decision' "
    "AND e.kind = 'serves'), '[]'::json) "
    "FROM goals g"
)
_DECISION_SELECT = (
    "SELECT d.id, d.action, d.dose, d.timing, d.frequency, d.duration, "
    "d.started_at, d.ended_at, d.note, " + _concept_refs_sql("decision", "d.id") + " "
    "FROM decisions d"
)
_MARKER_READING_SELECT = (
    "SELECT m.id, m.concept_id, c.name, m.value, m.unit, "
    "m.reference_low, m.reference_high, m.measured_at "
    "FROM markers m JOIN concepts c ON c.id = m.concept_id"
)


def _row_to_goal(r) -> Goal:
    return Goal(
        id=r[0],
        title=r[1],
        detail=r[2],
        concepts=[ConceptRef(id=c["id"], name=c["name"]) for c in r[3]],
        served_by=[DecisionRef(id=d["id"], action=d["action"]) for d in r[4]],
    )


def _row_to_decision(r) -> Decision:
    return Decision(
        id=r[0],
        action=r[1],
        dose=r[2],
        timing=r[3],
        frequency=r[4],
        duration=r[5],
        started_at=r[6],
        ended_at=r[7],
        note=r[8],
        concepts=[ConceptRef(id=c["id"], name=c["name"]) for c in r[9]],
    )


def _row_to_marker_reading(r) -> MarkerReading:
    return MarkerReading(
        id=r[0],
        concept=ConceptRef(id=r[1], name=r[2]),
        value=float(r[3]),
        unit=r[4],
        reference_low=float(r[5]) if r[5] is not None else None,
        reference_high=float(r[6]) if r[6] is not None else None,
        measured_at=r[7],
    )


# -- Impact engine shapes & renderings (issue #18) --------------------------
#
# The Impact engine weighs newly-arrived knowledge against owner anchors. A
# Claim/Protocol and a Decision/Goal/Marker each get a one-line `text` rendering
# for the `StanceJudge` to reason over; the inbox `Impact` carries both ends'
# labels so the Web App can render "new evidence <stance> your Decision X".


def _params_text(dose, timing, frequency, duration) -> str:
    """The structured parameters of a Protocol/Decision as a compact suffix."""
    params = ", ".join(v for v in (dose, timing, frequency, duration) if v)
    return f" ({params})" if params else ""


def _out_of_range(value, low, high) -> bool:
    if low is not None and value < low:
        return True
    if high is not None and value > high:
        return True
    return False


def _marker_label(name: str, value, unit: str, low, high) -> str:
    """A Marker reading rendered for an anchor: "apoB: 130 mg/dL (out of range)"."""
    flag = " (out of range)" if _out_of_range(value, low, high) else ""
    return f"{name}: {value} {unit}{flag}"


@dataclass(frozen=True)
class Impact:
    """A persisted Impact for the inbox (CONTEXT.md "Impact"; issue #18).

    Carries a readable label for both ends — the `source` Claim/Protocol that
    triggered it and the `anchor` Decision/Goal/Marker it bears on — so the inbox
    needs no second read to render the finding. `state` is the lifecycle position
    (`new → reviewed → actioned | dismissed`); `actioned_decision_id` records the
    Decision an `actioned` Impact produced (CONTEXT.md), or ``None``.
    """

    id: int
    source_type: str
    source_id: int
    source_label: str
    anchor_type: str
    anchor_id: int
    anchor_label: str
    stance: str
    state: str
    detail: str | None
    actioned_decision_id: int | None
    created_at: datetime
    tier: int = 1


# The inbox projection: an Impact with both polymorphic ends' labels resolved by
# LEFT JOINs (only the matching side is non-NULL). The marker label is built in the
# mapper from its parts, so the SQL stays free of numeric string-building. A
# `relation` source and a `concept` anchor (ADR-0013) are resolved the same way; a
# relationship that has since eroded LEFT-JOINs to NULL and falls back to `detail`.
_IMPACT_SELECT = (
    "SELECT i.id, i.source_type, i.source_id, scl.text, sp.action, "
    "       i.anchor_type, i.anchor_id, ad.action, ag.title, "
    "       ac.name, am.value, am.unit, am.reference_low, am.reference_high, "
    "       i.stance, i.state, i.detail, i.actioned_decision_id, i.created_at, "
    "       i.tier, srsrc.name, sr.predicate, srdst.name, acpt.name "
    "FROM impacts i "
    "LEFT JOIN claims scl ON i.source_type = 'claim' AND scl.id = i.source_id "
    "LEFT JOIN protocols sp ON i.source_type = 'protocol' AND sp.id = i.source_id "
    "LEFT JOIN concept_relations sr ON i.source_type = 'relation' AND sr.id = i.source_id "
    "LEFT JOIN concepts srsrc ON srsrc.id = sr.src_concept_id "
    "LEFT JOIN concepts srdst ON srdst.id = sr.dst_concept_id "
    "LEFT JOIN decisions ad ON i.anchor_type = 'decision' AND ad.id = i.anchor_id "
    "LEFT JOIN goals ag ON i.anchor_type = 'goal' AND ag.id = i.anchor_id "
    "LEFT JOIN markers am ON i.anchor_type = 'marker' AND am.id = i.anchor_id "
    "LEFT JOIN concepts ac ON i.anchor_type = 'marker' AND ac.id = am.concept_id "
    "LEFT JOIN concepts acpt ON i.anchor_type = 'concept' AND acpt.id = i.anchor_id"
)


def _relation_label(src: str, predicate: str, dst: str) -> str:
    """A lateral relationship rendered for the inbox: "APOE4 risk_factor_for Alzheimer's"."""
    return f"{src} {predicate} {dst}"


def _row_to_impact(r) -> Impact:
    if r[5] == "decision":
        anchor_label = r[7]
    elif r[5] == "goal":
        anchor_label = r[8]
    elif r[5] == "concept":
        anchor_label = r[23]
    else:  # marker
        anchor_label = _marker_label(r[9], r[10], r[11], r[12], r[13])
    if r[1] == "claim":
        source_label = r[3]
    elif r[1] == "protocol":
        source_label = r[4]
    elif r[1] == "relation":
        # An eroded relationship's row is gone (LEFT JOIN NULL) -> use the detail.
        source_label = (
            _relation_label(r[20], r[21], r[22]) if r[20] is not None
            else (r[16] or "(removed relationship)")
        )
    else:  # 'concept' source (a scope-widening summary)
        source_label = r[16] or ""
    return Impact(
        id=r[0],
        source_type=r[1],
        source_id=r[2],
        source_label=source_label,
        anchor_type=r[5],
        anchor_id=r[6],
        anchor_label=anchor_label,
        stance=r[14],
        state=r[15],
        detail=r[16],
        actioned_decision_id=r[17],
        created_at=r[18],
        tier=r[19],
    )


class Repository:
    """Thin data-access layer over Postgres for the slice-1 tables."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def commit(self) -> None:
        """Commit the current transaction — the job's durability boundary."""
        self._conn.commit()

    def rollback(self) -> None:
        """Discard the current transaction's uncommitted work.

        The daily job calls this when one Creator or video errors, so the failure
        leaves nothing half-written and the run continues with the rest already
        durably committed (PRD #1, user story 25).
        """
        self._conn.rollback()

    # -- reads ---------------------------------------------------------------

    def list_creators(self) -> list[CreatorIdentity]:
        """Return every *subscribed* Creator's stable identity, oldest first.

        This is the watch list the daily job reads to know whom to poll
        (PRD #1, user story 5). Unsubscribed one-off Creators — created only to
        attribute a "Process me" playlist video (issue #69) — are excluded, so they
        are never polled and never surface in `/api/creators`.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT channel_id, name FROM creators "
                "WHERE subscribed ORDER BY created_at, id"
            )
            return [CreatorIdentity(channel_id=r[0], name=r[1]) for r in cur.fetchall()]

    def creator_id(self, channel_id: str, *, subscribed_only: bool = False) -> int | None:
        """The internal id of a Creator by its stable channel_id, or None.

        Lets a Web App backfill trigger re-run population for one Creator without
        re-resolving its @handle (issue #15). Pass `subscribed_only=True` to resolve
        only watch-list Creators, so a one-off Creator (issue #69) is treated as
        absent and is never backfilled; the default resolves any Creator (e.g. for
        trust-tiering, which applies to one-off Creators too).
        """
        sql = "SELECT id FROM creators WHERE channel_id = %s"
        if subscribed_only:
            sql += " AND subscribed"
        with self._conn.cursor() as cur:
            cur.execute(sql, (channel_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def list_candidates(self) -> list[StoredCandidate]:
        """Every stored backfill Candidate, newest published first.

        The raw storage view — what backfill persisted, regardless of any later
        owner decision. The backfill tests assert on it to confirm only metadata is
        stored and the cutoff is honored; the Web App's review queue instead reads
        `list_backfill_candidates`, which hides Candidates already acted on.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.video_id, cr.channel_id, c.title, c.description, "
                "       c.url, c.published_at, cr.name "
                "FROM candidates c JOIN creators cr ON cr.id = c.creator_id "
                "ORDER BY c.published_at DESC, c.video_id"
            )
            return [
                StoredCandidate(
                    video_id=r[0],
                    channel_id=r[1],
                    title=r[2],
                    description=r[3],
                    url=r[4],
                    published_at=r[5],
                    channel_name=r[6],
                )
                for r in cur.fetchall()
            ]

    def list_backfill_candidates(
        self, *, newest_first: bool = True
    ) -> list[StoredCandidate]:
        """Backfill Candidates awaiting the owner's decision, sorted by publish date.

        The Web App's backfill review queue (issue #15): metadata-only Candidates
        the owner can approve into the Body of Knowledge or bulk-reject. Mirrors
        `list_daily_candidates`' filter — a Candidate shows until it is admitted or
        rejected, and a `failed` one stays visible so it can be retried; approved
        and processing ones show their in-flight state. Each carries its Creator's
        name and current lifecycle `state`.

        `newest_first` (the default) sorts most-recently-published first; pass False
        to flip to oldest-first (issue #31). The sort is on `published_at`, which the
        lazy detail fetch corrects, so the ordering sharpens as Candidates are fetched.
        """
        direction = "DESC" if newest_first else "ASC"
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.video_id, cr.channel_id, c.title, c.description, c.url, "
                "       c.published_at, cr.name, COALESCE(a.state, %s) AS state "
                "FROM candidates c "
                "JOIN creators cr ON cr.id = c.creator_id "
                "LEFT JOIN admissions a ON a.video_id = c.video_id "
                "WHERE COALESCE(a.state, %s) IN "
                "      (%s, 'approved', 'processing', 'failed') "
                f"ORDER BY c.published_at {direction}, c.video_id",
                (CANDIDATE, CANDIDATE, CANDIDATE),
            )
            return [
                StoredCandidate(
                    video_id=r[0],
                    channel_id=r[1],
                    title=r[2],
                    description=r[3],
                    url=r[4],
                    published_at=r[5],
                    channel_name=r[6],
                    state=r[7],
                )
                for r in cur.fetchall()
            ]

    def get_backfill_candidate(self, video_id: str) -> StoredCandidate | None:
        """One backfill Candidate by video_id, or None — the single-row read for the
        lazy detail fetch (issue #31), so the API can return the just-updated Candidate
        in the same shape the queue lists. Mirrors `list_backfill_candidates`' join for
        the Creator name and lifecycle `state`, but is not filtered by state."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.video_id, cr.channel_id, c.title, c.description, c.url, "
                "       c.published_at, cr.name, COALESCE(a.state, %s) AS state "
                "FROM candidates c "
                "JOIN creators cr ON cr.id = c.creator_id "
                "LEFT JOIN admissions a ON a.video_id = c.video_id "
                "WHERE c.video_id = %s",
                (CANDIDATE, video_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return StoredCandidate(
            video_id=row[0],
            channel_id=row[1],
            title=row[2],
            description=row[3],
            url=row[4],
            published_at=row[5],
            channel_name=row[6],
            state=row[7],
        )

    def processed_video_ids(self) -> set[str]:
        """The set of videos whose Transcript and Summary are both persisted.

        The daily job diffs each Creator's freshly-discovered feed against this
        set to find genuinely new uploads; a video here is never re-fetched or
        re-summarized, which is what makes a repeat run idempotent (user stories
        6, 23). A video only enters the set once `summarized_at` is stamped, so a
        prior run that archived a Transcript but crashed before summarizing is
        retried rather than skipped.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT video_id FROM processing_state WHERE summarized_at IS NOT NULL"
            )
            return {row[0] for row in cur.fetchall()}

    def known_video_ids(self) -> set[str]:
        """Every external video_id the system already knows in *any* form — an
        archived/processing video, a backfill Candidate, or an admission-lifecycle row.

        The "Process me" playlist dedup set (issue #69): a one-off video already known
        as a video, a Candidate, or an admission is skipped, so re-running the job
        never reprocesses and a playlist video that overlaps a watched Creator's
        catalogue is never duplicated. Broader than `processed_video_ids()` — which is
        only the daily-diff set of fully-summarized videos — because a one-off video
        must also defer to metadata-only Candidates and to videos still in flight.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT video_id FROM videos "
                "UNION SELECT video_id FROM candidates "
                "UNION SELECT video_id FROM admissions"
            )
            return {row[0] for row in cur.fetchall()}

    def unsent_summaries(self) -> list[ArchivedSummary]:
        """Every processed video whose Summary has not yet gone out in a Digest.

        Returned oldest-published first for a chronological Digest. Bundles the
        run's new Summaries together with any left unsent by an earlier failed
        send, so a retry picks them all up without re-summarizing (user stories
        18, 24). Each video contributes its latest Summary.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT video_id, title, url, body FROM ("
                "  SELECT DISTINCT ON (v.video_id) "
                "         v.video_id, v.title, v.url, s.body, v.published_at "
                "  FROM processing_state ps "
                "  JOIN videos v ON v.video_id = ps.video_id "
                "  JOIN summaries s ON s.video_id = v.video_id "
                "  WHERE ps.summarized_at IS NOT NULL "
                "    AND ps.digest_sent_at IS NULL "
                "  ORDER BY v.video_id, s.created_at DESC, s.id DESC"
                ") latest ORDER BY published_at, video_id"
            )
            return [
                ArchivedSummary(video_id=r[0], title=r[1], url=r[2], body=r[3])
                for r in cur.fetchall()
            ]

    def get_summary(self, video_id: str) -> ArchivedSummary | None:
        """Return the latest persisted Summary for a video, or None."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT v.video_id, v.title, v.url, s.body "
                "FROM summaries s JOIN videos v ON v.video_id = s.video_id "
                "WHERE s.video_id = %s ORDER BY s.created_at DESC, s.id DESC "
                "LIMIT 1",
                (video_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ArchivedSummary(video_id=row[0], title=row[1], url=row[2], body=row[3])

    def load_transcript_segments(self, video_id: str) -> list[TranscriptSegment]:
        """Read back the archived Transcript's timestamped segments."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT segments FROM transcripts WHERE video_id = %s",
                (video_id,),
            )
            row = cur.fetchone()
        if row is None:
            return []
        return [
            TranscriptSegment(text=s["text"], start=s["start"], duration=s["duration"])
            for s in row[0]
        ]

    # -- writes --------------------------------------------------------------

    def add_creator(self, identity: CreatorIdentity, *, subscribed: bool = True) -> int:
        """Persist a Creator by stable identity; idempotent on channel_id.

        Re-adding an existing Creator refreshes its display name but never
        creates a duplicate (PRD #1, user stories 3-4). Like the other writes,
        this does not commit — the caller owns the transaction boundary.

        `subscribed` applies only when the Creator is first inserted (issue #69):
        an explicit watch-list add (the default, `True`) also *promotes* an existing
        one-off Creator onto the watch list, while the archiving upsert
        (`subscribed=False`) creates a new one-off Creator not-subscribed and leaves
        an existing Creator's flag untouched — so processing a one-off video never
        un-subscribes a watched Creator.
        """
        # On conflict the watch-list add re-asserts `subscribed = TRUE` (promotion);
        # the one-off path updates only the name, never touching the flag.
        on_conflict = (
            "ON CONFLICT (channel_id) DO UPDATE SET name = EXCLUDED.name, subscribed = TRUE"
            if subscribed
            else "ON CONFLICT (channel_id) DO UPDATE SET name = EXCLUDED.name"
        )
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO creators (channel_id, name, subscribed) VALUES (%s, %s, %s) "
                + on_conflict
                + " RETURNING id",
                (identity.channel_id, identity.name, subscribed),
            )
            return cur.fetchone()[0]

    def add_candidate(self, creator_id: int, candidate: CandidateMetadata) -> bool:
        """Persist a metadata-only backfill Candidate; idempotent on video_id.

        Returns whether a row was actually inserted, so a re-run can report only
        genuinely new Candidates. No Transcript or Summary is written — a backfill
        Candidate is metadata only until the owner approves it (ADR-0004).
        Re-running backfill (e.g. re-adding the Creator) inserts nothing for a
        video already stored. Does not commit — the caller owns the transaction
        so a Creator and its Candidates land together.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO candidates (video_id, creator_id, url, title, "
                "description, published_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (video_id) DO NOTHING",
                (
                    candidate.video_id,
                    creator_id,
                    candidate.url,
                    candidate.title,
                    candidate.description,
                    candidate.published_at,
                ),
            )
            return cur.rowcount > 0

    def update_candidate_details(
        self, video_id: str, *, description: str, published_at: datetime
    ) -> bool:
        """Persist a lazily-fetched description + accurate publish date on a Candidate.

        The write half of the lazy detail fetch (issue #31): overwrites the Candidate's
        best-effort description and publish date with the real ones from a per-video
        extraction. Idempotent and safe to re-run — it updates the existing row in place,
        never inserting a duplicate — so a Candidate that already has details is simply
        refreshed. Returns whether a row matched. Does not commit — the caller owns the
        transaction.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE candidates SET description = %s, published_at = %s "
                "WHERE video_id = %s",
                (description, published_at, video_id),
            )
            return cur.rowcount > 0

    def remove_creator(self, channel_id: str) -> bool:
        """Drop a Creator from the watch list; return whether a row was removed."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM creators WHERE channel_id = %s", (channel_id,))
            return cur.rowcount > 0

    def archive_transcript(
        self, fetched: FetchedTranscript, *, retrieved_at: datetime
    ) -> None:
        """Immutably archive a Transcript with full provenance (ADR-0001).

        Resolves/creates the Creator, records the video's provenance, stores the
        timestamped segments, and opens the processing-state row — all in the
        caller's transaction.
        """
        prov = fetched.provenance
        creator_id = self._upsert_creator(prov)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO videos (video_id, creator_id, url, title, "
                "published_at, retrieved_at, transcript_source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (video_id) DO NOTHING",
                (
                    prov.video_id,
                    creator_id,
                    prov.url,
                    prov.title,
                    prov.published_at,
                    retrieved_at,
                    fetched.source,
                ),
            )
            segments_json = json.dumps(
                [
                    {"text": s.text, "start": s.start, "duration": s.duration}
                    for s in fetched.segments
                ]
            )
            cur.execute(
                "INSERT INTO transcripts (video_id, segments) VALUES (%s, %s) "
                "ON CONFLICT (video_id) DO NOTHING",
                (prov.video_id, segments_json),
            )
            cur.execute(
                "INSERT INTO processing_state (video_id, transcript_archived_at) "
                "VALUES (%s, %s) ON CONFLICT (video_id) DO NOTHING",
                (prov.video_id, retrieved_at),
            )

    def save_summary(
        self, video_id: str, body: str, *, model: str, summarized_at: datetime
    ) -> None:
        """Persist a Summary alongside its Transcript and mark the video processed."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO summaries (video_id, body, model) VALUES (%s, %s, %s)",
                (video_id, body, model),
            )
            cur.execute(
                "UPDATE processing_state SET summarized_at = %s WHERE video_id = %s",
                (summarized_at, video_id),
            )

    def mark_digest_sent(self, video_ids: list[str], *, sent_at: datetime) -> None:
        """Record that each video's Summary went out in a Digest."""
        if not video_ids:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE processing_state SET digest_sent_at = %s "
                "WHERE video_id = ANY(%s)",
                (sent_at, video_ids),
            )

    # == Part 2: review queue, jobs & the Body of Knowledge ==================
    #
    # The Web App reads the daily-Candidate queue; approval enqueues a job the
    # worker drains, walking the Candidate approved → processing → admitted, and
    # on admission writes the extracted Claims/Protocols/Concepts/edges (ADR-0008,
    # ADR-0009, ADR-0010). As above, none of these commit — the API request and
    # the worker each own their transaction boundary.

    # -- review queue (reads) ------------------------------------------------

    def list_daily_candidates(self) -> list[DailyCandidate]:
        """Daily Candidates awaiting the owner's decision, newest published first.

        A daily Candidate is a processed video (Transcript + Summary) not yet
        admitted or rejected: its admission row is absent (a plain `candidate`) or
        in flight (`approved`/`processing`/`failed`). Admitted videos have moved
        into the Body of Knowledge and rejected ones are declined, so neither
        shows here. Each carries its latest Summary for review (ADR-0007).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT v.video_id, v.title, v.url, "
                "       COALESCE(a.state, %s) AS state, v.published_at, s.body "
                "FROM videos v "
                "JOIN LATERAL ("
                "  SELECT body FROM summaries s WHERE s.video_id = v.video_id "
                "  ORDER BY s.created_at DESC, s.id DESC LIMIT 1"
                ") s ON TRUE "
                "LEFT JOIN admissions a ON a.video_id = v.video_id "
                "WHERE COALESCE(a.state, %s) IN "
                "      (%s, 'approved', 'processing', 'failed') "
                "ORDER BY v.published_at DESC, v.video_id",
                (CANDIDATE, CANDIDATE, CANDIDATE),
            )
            return [
                DailyCandidate(
                    video_id=r[0],
                    title=r[1],
                    url=r[2],
                    state=r[3],
                    published_at=r[4],
                    summary=r[5],
                )
                for r in cur.fetchall()
            ]

    def list_processed_videos(self) -> list[ProcessedVideo]:
        """Processed videos that reached a terminal admission, newest-added first
        (issue #33).

        Backs the read-only Logs page: a record of what the pipeline has carried into
        the Body of Knowledge. A row appears only once a processed video (Transcript +
        Summary archived) has reached a *terminal* admission state — `admitted` (in
        the Body of Knowledge) or `failed` (extraction errored). Videos still in flight
        or never acted on (no admission row, `approved`/`processing`/`rejected`) are
        deliberately hidden: the owner asked the log to show only what was admitted or
        failed, not the daily review backlog. One query: videos ⋈ creators ⋈ latest
        Summary ⋈ admission state. Ordered by `retrieved_at` (when the system pulled it
        in — the "date added") so the newest record is first; `bok_state` is the
        admission state, always `admitted` or `failed` here.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT v.video_id, v.title, cr.name, v.retrieved_at, s.body, "
                "       a.state AS bok_state "
                "FROM processing_state ps "
                "JOIN videos v ON v.video_id = ps.video_id "
                "JOIN creators cr ON cr.id = v.creator_id "
                "JOIN admissions a ON a.video_id = v.video_id "
                "JOIN LATERAL ("
                "  SELECT body FROM summaries s WHERE s.video_id = v.video_id "
                "  ORDER BY s.created_at DESC, s.id DESC LIMIT 1"
                ") s ON TRUE "
                "WHERE ps.summarized_at IS NOT NULL "
                "  AND a.state IN ('admitted', 'failed') "
                "ORDER BY v.retrieved_at DESC, v.video_id"
            )
            return [
                ProcessedVideo(
                    video_id=r[0],
                    title=r[1],
                    creator_name=r[2],
                    added_at=r[3],
                    summary=r[4],
                    bok_state=r[5],
                )
                for r in cur.fetchall()
            ]

    def admission_state(self, video_id: str) -> str:
        """The Candidate's lifecycle state — `candidate` when no row exists yet."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM admissions WHERE video_id = %s", (video_id,)
            )
            row = cur.fetchone()
        return row[0] if row else CANDIDATE

    def admitted_video_ids(self) -> list[str]:
        """Every video that has reached the `admitted` state, oldest first.

        The set the relationship reprocess walks (issue #64): each one's archived
        Transcript is re-extracted to re-project its Claims' triples into lateral
        relationships. Ordered deterministically so an interrupted run resumes in
        the same order it left off.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT video_id FROM admissions WHERE state = 'admitted' "
                "ORDER BY video_id"
            )
            return [r[0] for r in cur.fetchall()]

    def reprocessed_video_ids(self) -> set[str]:
        """Videos whose relationship re-extraction has already completed (issue #64).

        Read once at the start of a reprocess run so completed videos are skipped —
        this is what makes the batch resumable and a second full run a no-op.
        """
        with self._conn.cursor() as cur:
            cur.execute("SELECT video_id FROM relationship_reprocess")
            return {r[0] for r in cur.fetchall()}

    def mark_reprocessed(self, video_id: str) -> None:
        """Record that a video's relationship re-extraction has completed (issue #64).

        Written in the *same* transaction as the supersede it confirms, so the
        progress marker and the re-projected relationships commit atomically — an
        interrupt either leaves both done or neither. Idempotent on video_id.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO relationship_reprocess (video_id) VALUES (%s) "
                "ON CONFLICT (video_id) DO NOTHING",
                (video_id,),
            )

    def load_fetched_transcript(self, video_id: str) -> FetchedTranscript | None:
        """Reassemble the archived Transcript + provenance for extraction.

        The Extractor needs the full Transcript and its provenance; this reads the
        immutable archive back into the same `FetchedTranscript` the daily job
        passed in. Returns ``None`` if the video has no archived Transcript.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT v.video_id, v.title, c.channel_id, c.name, v.published_at, "
                "       v.transcript_source, t.segments "
                "FROM videos v "
                "JOIN creators c ON c.id = v.creator_id "
                "JOIN transcripts t ON t.video_id = v.video_id "
                "WHERE v.video_id = %s",
                (video_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        provenance = Provenance(
            video_id=row[0],
            title=row[1],
            channel_id=row[2],
            channel_name=row[3],
            published_at=row[4],
        )
        segments = [
            TranscriptSegment(text=s["text"], start=s["start"], duration=s["duration"])
            for s in row[6]
        ]
        return FetchedTranscript(
            provenance=provenance, segments=segments, source=row[5]
        )

    # -- lifecycle & job queue (writes) --------------------------------------

    def set_admission(self, video_id: str, state: str, *, error: str | None = None) -> None:
        """Move a Candidate to a lifecycle state (CONTEXT.md; ADR-0004, ADR-0010)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admissions (video_id, state, error) VALUES (%s, %s, %s) "
                "ON CONFLICT (video_id) DO UPDATE "
                "SET state = EXCLUDED.state, error = EXCLUDED.error, updated_at = now()",
                (video_id, state, error),
            )

    def enqueue_job(self, video_id: str, *, kind: str = "admit") -> int:
        """Enqueue background work for the worker to drain (ADR-0009)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (kind, video_id) VALUES (%s, %s) RETURNING id",
                (kind, video_id),
            )
            return cur.fetchone()[0]

    def cancel_queued_jobs(self, video_id: str) -> None:
        """Drop a video's not-yet-started jobs — e.g. when the owner rejects it."""
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs WHERE video_id = %s AND state = 'queued'",
                (video_id,),
            )

    def claim_next_job(self) -> QueuedJob | None:
        """Atomically claim the next queued job, marking it `running`.

        Uses `FOR UPDATE SKIP LOCKED` so concurrent workers never grab the same
        job (ADR-0009). The claim and the `running` mark share the caller's
        transaction; committing releases the row lock with the job already off the
        queue. Returns ``None`` when the queue is empty.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, video_id, attempts FROM jobs "
                "WHERE state = 'queued' ORDER BY id "
                "FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            row = cur.fetchone()
            if row is None:
                # End the read-only transaction the SELECT opened: an empty queue
                # is the common poll, and the worker then sleeps before re-polling.
                # Without this the `FOR UPDATE` snapshot leaves the connection idle
                # in transaction, holding a lock on `jobs` that blocks every
                # service's `init_schema` DDL on its next boot (the API would hang
                # in startup forever). Committing/rolling back releases it at once.
                self._conn.rollback()
                return None
            cur.execute(
                "UPDATE jobs SET state = 'running', attempts = attempts + 1, "
                "updated_at = now() WHERE id = %s",
                (row[0],),
            )
        return QueuedJob(id=row[0], kind=row[1], video_id=row[2], attempts=row[3] + 1)

    def mark_job_done(self, job_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET state = 'done', updated_at = now() WHERE id = %s",
                (job_id,),
            )

    def mark_job_failed(self, job_id: int, *, error: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET state = 'failed', last_error = %s, updated_at = now() "
                "WHERE id = %s",
                (error, job_id),
            )

    # -- Body of Knowledge (writes) ------------------------------------------

    def add_claim(
        self, video_id: str, *, text: str, type: str, locator_seconds: int
    ) -> int:
        """Persist an admitted Claim attributed to its Source video (ADR-0002)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO claims (video_id, text, type, locator_seconds) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (video_id, text, type, locator_seconds),
            )
            return cur.fetchone()[0]

    def claim_ids_for_video(
        self, video_id: str, *, include_protected: bool = True
    ) -> list[int]:
        """A video's Claim ids — the transcript span re-extraction supersedes (ADR-0005).

        Re-extraction versions Claims *within one transcript span* (the video),
        replacing the prior ones with a fresh extraction. A *protected* (owner-edited)
        Claim is a hand-corrected version a supersede pass must never clobber
        (ADR-0010), so `include_protected=False` excludes it from the span being
        superseded. Ordered for deterministic supersede.
        """
        sql = "SELECT id FROM claims WHERE video_id = %s"
        if not include_protected:
            sql += " AND NOT protected"
        sql += " ORDER BY id"
        with self._conn.cursor() as cur:
            cur.execute(sql, (video_id,))
            return [r[0] for r in cur.fetchall()]

    def add_protocol(
        self,
        video_id: str,
        *,
        action: str,
        dose: str | None,
        timing: str | None,
        frequency: str | None,
        duration: str | None,
        locator_seconds: int,
    ) -> int:
        """Persist a structured Protocol (ADR-0010); the DB CHECK enforces structure."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO protocols (video_id, action, dose, timing, frequency, "
                "duration, locator_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (video_id, action, dose, timing, frequency, duration, locator_seconds),
            )
            return cur.fetchone()[0]

    def add_concept(self, name: str, *, kind: str | None = None) -> int:
        """Mint a new Concept hub node (ADR-0008)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO concepts (name, kind) VALUES (%s, %s) RETURNING id",
                (name, kind),
            )
            return cur.fetchone()[0]

    def add_embedding(
        self, owner_type: str, owner_id: int, embedding: list[float], *, model: str
    ) -> None:
        """Append a model-stamped embedding over the extracted layer (ADR-0008)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO embeddings (owner_type, owner_id, embedding, model) "
                "VALUES (%s, %s, %s::vector, %s)",
                (owner_type, owner_id, _vector_literal(embedding), model),
            )

    def add_edge(
        self,
        src_type: str,
        src_id: int,
        dst_type: str,
        dst_id: int,
        kind: str,
        *,
        props: dict | None = None,
    ) -> None:
        """Assert a graph edge, idempotently (ADR-0008).

        The unique constraint makes re-extraction re-assert the same edge without
        dup-checking; the integrity trigger rejects an endpoint that does not
        exist, so no dangling edges accumulate.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO edges (src_type, src_id, dst_type, dst_id, kind, props) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (src_type, src_id, dst_type, dst_id, kind) DO NOTHING",
                (src_type, src_id, dst_type, dst_id, kind, json.dumps(props or {})),
            )

    # -- Lateral relationships: claim-grounded Concept→Concept links (ADR-0013) --
    #
    # A relationship is a *materialized projection of Claims*, derived at admit time
    # and self-healing on supersede/delete. `add_concept_relation` upserts the
    # directed edge and records the evidencing Claim; deletes prune any relationship
    # left with no evidence (the `eroded` event slice-4 alerting hangs off).

    def add_concept_relation(
        self,
        src_concept_id: int,
        predicate: str,
        dst_concept_id: int,
        *,
        claim_id: int,
    ) -> int:
        """Derive (or re-assert) a lateral relationship and link its evidencing Claim.

        Idempotent on both halves (ADR-0005): the UNIQUE on (src, predicate, dst)
        collapses re-admission onto one relationship row, and the evidence link's
        composite PK collapses a re-asserted Claim onto one evidence row. Returns
        the relationship id. The caller guards against self-loops (the DB CHECK
        also rejects them).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO concept_relations (src_concept_id, predicate, dst_concept_id) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (src_concept_id, predicate, dst_concept_id) DO UPDATE "
                "  SET src_concept_id = EXCLUDED.src_concept_id "  # no-op to return id
                "RETURNING id",
                (src_concept_id, predicate, dst_concept_id),
            )
            relation_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO concept_relation_evidence (relation_id, claim_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (relation_id, claim_id),
            )
        return relation_id

    def prune_orphaned_relations(self, relation_ids: list[int]) -> list[ConceptRelation]:
        """Remove any of `relation_ids` left with no evidencing Claim (ADR-0013).

        Called on the supersede/delete path after a Claim's evidence links have
        gone (they cascade with the Claim): a relationship whose last evidencing
        Claim disappeared no longer rests on anything the owner's library says, so
        it is removed rather than left asserting a connection no Claim supports. The
        removed relationships are returned so the caller can raise an `eroded`
        Impact (slice-4 alerting) instead of letting them vanish silently.
        """
        if not relation_ids:
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cr.id, cr.src_concept_id, src.name, cr.predicate, "
                "       cr.dst_concept_id, dst.name "
                "FROM concept_relations cr "
                "JOIN concepts src ON src.id = cr.src_concept_id "
                "JOIN concepts dst ON dst.id = cr.dst_concept_id "
                "WHERE cr.id = ANY(%s) AND NOT EXISTS ("
                "  SELECT 1 FROM concept_relation_evidence e WHERE e.relation_id = cr.id)",
                (relation_ids,),
            )
            orphaned = [
                ConceptRelation(
                    id=r[0],
                    src_concept_id=r[1],
                    src_name=r[2],
                    predicate=r[3],
                    dst_concept_id=r[4],
                    dst_name=r[5],
                    evidence_claim_ids=[],
                )
                for r in cur.fetchall()
            ]
            if orphaned:
                cur.execute(
                    "DELETE FROM concept_relations WHERE id = ANY(%s)",
                    ([rel.id for rel in orphaned],),
                )
        return orphaned

    def relations_evidenced_by(self, claim_id: int) -> list[int]:
        """The ids of relationships a Claim currently evidences (ADR-0013).

        Captured *before* a Claim is deleted/superseded so the surviving relations
        can be re-checked for orphanhood afterwards (`prune_orphaned_relations`).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT relation_id FROM concept_relation_evidence WHERE claim_id = %s",
                (claim_id,),
            )
            return [r[0] for r in cur.fetchall()]

    def relations_evidenced_by_claim_detailed(self, claim_id: int) -> list[ConceptRelation]:
        """The relationships a Claim evidences, with endpoints resolved (ADR-0013).

        Captured *before* a delete/supersede so the alerting layer can tell which of
        them erode (lose their last evidence) and on which Concepts, after the Claim
        and its evidence links are gone.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cr.id, cr.src_concept_id, src.name, cr.predicate, "
                "       cr.dst_concept_id, dst.name "
                "FROM concept_relation_evidence cre "
                "JOIN concept_relations cr ON cr.id = cre.relation_id "
                "JOIN concepts src ON src.id = cr.src_concept_id "
                "JOIN concepts dst ON dst.id = cr.dst_concept_id "
                "WHERE cre.claim_id = %s ORDER BY cr.id",
                (claim_id,),
            )
            return [
                ConceptRelation(
                    id=r[0], src_concept_id=r[1], src_name=r[2], predicate=r[3],
                    dst_concept_id=r[4], dst_name=r[5], evidence_claim_ids=[],
                )
                for r in cur.fetchall()
            ]

    def existing_relation_ids(self, relation_ids: list[int]) -> set[int]:
        """Which of `relation_ids` still exist — the survivors of a prune (ADR-0013)."""
        if not relation_ids:
            return set()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM concept_relations WHERE id = ANY(%s)", (relation_ids,)
            )
            return {r[0] for r in cur.fetchall()}

    def list_concept_relations(self) -> list[ConceptRelation]:
        """Every lateral relationship with its evidencing Claim ids (ADR-0013).

        A whole-graph read used by tests and the relationship browser; the
        neighbourhood view (slice 2) layers ranking and subtree roll-up on top.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cr.id, cr.src_concept_id, src.name, cr.predicate, "
                "       cr.dst_concept_id, dst.name, "
                "       ARRAY(SELECT claim_id FROM concept_relation_evidence e "
                "             WHERE e.relation_id = cr.id ORDER BY claim_id) "
                "FROM concept_relations cr "
                "JOIN concepts src ON src.id = cr.src_concept_id "
                "JOIN concepts dst ON dst.id = cr.dst_concept_id "
                "ORDER BY cr.id",
            )
            return [
                ConceptRelation(
                    id=r[0],
                    src_concept_id=r[1],
                    src_name=r[2],
                    predicate=r[3],
                    dst_concept_id=r[4],
                    dst_name=r[5],
                    evidence_claim_ids=list(r[6]),
                )
                for r in cur.fetchall()
            ]

    def contested_pair(
        self, src_concept_id: int, dst_concept_id: int
    ) -> ContestedPair | None:
        """Whether one directed Concept pair is contested, and which predicates clash.

        For the ordered (src, dst) pair, collect every distinct `predicate` the
        owner's Claims assert between them and derive the `tensions` among them
        (ADR-0013): an opposite signed pair, or `no_effect_on` against any signed
        predicate. Returns ``None`` when no relationship links the pair in that
        direction — there is nothing to contest. The verdict is *derived* from the
        materialized relationships, never a merge (ADR-0002): both predicates remain
        evidenced, the pair is merely flagged.

        Directed to match the contradiction rule itself, which is defined on the
        *same ordered pair* — "src protects_against dst" and "dst risk_factor_for
        src" are different claims about different directions, not a disagreement.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT src.name, dst.name, array_agg(DISTINCT cr.predicate) "
                "FROM concept_relations cr "
                "JOIN concepts src ON src.id = cr.src_concept_id "
                "JOIN concepts dst ON dst.id = cr.dst_concept_id "
                "WHERE cr.src_concept_id = %s AND cr.dst_concept_id = %s "
                "GROUP BY src.name, dst.name",
                (src_concept_id, dst_concept_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        predicates = sorted(row[2])
        return ContestedPair(
            src_concept_id=src_concept_id,
            src_name=row[0],
            dst_concept_id=dst_concept_id,
            dst_name=row[1],
            predicates=predicates,
            tensions=tensions(predicates),
        )

    def descendant_concept_ids(self, concept_id: int) -> list[int]:
        """A Concept and every Concept under it in the confirmed `broader-of` DAG.

        The subtree the roll-up neighbourhood spans (ADR-0013): the anchor plus all
        narrower Concepts reachable by following *confirmed* `broader-of` edges
        (`broader --broader-of--> narrower`). A proposed-but-unconfirmed edge is
        invisible here, so an unconfirmed parent never silently pulls a subtree into
        view (user story 19). The recursive walk is cycle-safe by construction (the
        edge cycle-guard forbids loops) and deduped, so a Concept reachable by two
        paths through a DAG diamond appears once. Until hierarchy lands (slice 3)
        no `broader-of` edge exists and this is just `[concept_id]`.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "WITH RECURSIVE subtree(id) AS ("
                "    SELECT %s::bigint "
                "  UNION "
                "    SELECT e.dst_id FROM edges e JOIN subtree s ON e.src_id = s.id "
                "    WHERE e.kind = 'broader-of' AND e.src_type = 'concept' "
                "      AND e.dst_type = 'concept' "
                "      AND COALESCE(e.props->>'confirmed', 'false') = 'true' "
                ") SELECT id FROM subtree",
                (concept_id,),
            )
            return [r[0] for r in cur.fetchall()]

    def ancestor_concept_ids(self, concept_id: int) -> list[int]:
        """A Concept and every Concept *above* it in the confirmed `broader-of` DAG.

        The mirror of `descendant_concept_ids`, walking confirmed `broader-of` edges
        upward (narrower → broader). Used by relationship alerting (ADR-0013): a
        relationship touching Concept X is relevant to a Goal/Decision tracking *any*
        ancestor of X, so tracking "Brain" catches a development on "Brain
        metabolism" without tracking every leaf (user story 31).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "WITH RECURSIVE supertree(id) AS ("
                "    SELECT %s::bigint "
                "  UNION "
                "    SELECT e.src_id FROM edges e JOIN supertree s ON e.dst_id = s.id "
                "    WHERE e.kind = 'broader-of' AND e.src_type = 'concept' "
                "      AND e.dst_type = 'concept' "
                "      AND COALESCE(e.props->>'confirmed', 'false') = 'true' "
                ") SELECT id FROM supertree",
                (concept_id,),
            )
            return [r[0] for r in cur.fetchall()]

    def concept_neighbourhood(
        self,
        concept_id: int,
        *,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        now: datetime | None = None,
    ) -> Neighbourhood | None:
        """The roll-up neighbourhood of a Concept (ADR-0013): sub-Concepts + relationships.

        Returns the selected Concept's sub-Concepts and every lateral relationship
        in its whole `broader-of` subtree — surfaced at the selected Concept,
        attributed to the descendant it came from (`via_*`), deduped across DAG
        diamonds, and ranked by evidence Strength (distinct creators × trust-tier ×
        recency). A relationship is flagged `contested` when an opposite or
        `no_effect_on` predicate also holds on the same ordered pair. ``None`` if
        the Concept does not exist.
        """
        now = now or datetime.now(timezone.utc)
        name = self._concept_name(concept_id)
        if name is None:
            return None

        subtree = self.descendant_concept_ids(concept_id)
        relations = self._neighbour_relations(
            concept_id, subtree, half_life_days=half_life_days, now=now
        )
        return Neighbourhood(
            concept_id=concept_id,
            concept_name=name,
            sub_concepts=self._sub_concepts(concept_id, subtree),
            relations=relations,
        )

    def _concept_name(self, concept_id: int) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT name FROM concepts WHERE id = %s", (concept_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def _sub_concepts(self, anchor_id: int, subtree: list[int]) -> list[ConceptRef]:
        """The anchor's sub-Concepts (its confirmed `broader-of` descendants)."""
        ids = [cid for cid in subtree if cid != anchor_id]
        if not ids:
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM concepts WHERE id = ANY(%s) ORDER BY name",
                (ids,),
            )
            return [ConceptRef(id=r[0], name=r[1]) for r in cur.fetchall()]

    def _neighbour_relations(
        self,
        anchor_id: int,
        subtree: list[int],
        *,
        half_life_days: float,
        now: datetime,
    ) -> list[NeighbourRelation]:
        """Every relationship touching the subtree, with Strength, contested flag, via."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cr.id, cr.src_concept_id, src.name, cr.predicate, "
                "       cr.dst_concept_id, dst.name, "
                "       cre.claim_id, v.creator_id, creators.trust_tier, v.published_at, "
                "       cl.text, cl.type, cl.locator_seconds, v.url, v.title "
                "FROM concept_relations cr "
                "JOIN concepts src ON src.id = cr.src_concept_id "
                "JOIN concepts dst ON dst.id = cr.dst_concept_id "
                "JOIN concept_relation_evidence cre ON cre.relation_id = cr.id "
                "JOIN claims cl ON cl.id = cre.claim_id "
                "JOIN videos v ON v.video_id = cl.video_id "
                "JOIN creators ON creators.id = v.creator_id "
                "WHERE cr.src_concept_id = ANY(%(ids)s) OR cr.dst_concept_id = ANY(%(ids)s)",
                {"ids": subtree},
            )
            rows = cur.fetchall()

        subtree_set = set(subtree)
        # Group evidence rows by relation, accumulating Strength contributions.
        grouped: dict[int, dict] = {}
        for r in rows:
            rel = grouped.setdefault(
                r[0],
                {
                    "src_id": r[1], "src_name": r[2], "predicate": r[3],
                    "dst_id": r[4], "dst_name": r[5],
                    "claim_ids": set(), "contribs": [], "evidence": {},
                },
            )
            rel["claim_ids"].add(r[6])
            rel["contribs"].append(
                EvidenceContribution(creator_id=r[7], trust_tier=r[8], dated=r[9])
            )
            # One Citation per distinct evidencing Claim — the same shape NL Query
            # cites, clickable through to its Source + locator (ADR-0011, ADR-0013).
            rel["evidence"].setdefault(
                r[6],
                Citation(
                    claim_id=r[6], text=r[10], type=r[11],
                    deep_link=locator_url(r[13], r[12]), source_title=r[14],
                ),
            )

        # Contested: another predicate on the *same ordered pair* contradicts this one.
        pair_predicates: dict[tuple[int, int], set[str]] = {}
        for rel in grouped.values():
            pair_predicates.setdefault((rel["src_id"], rel["dst_id"]), set()).add(
                rel["predicate"]
            )

        result: list[NeighbourRelation] = []
        for relation_id, rel in grouped.items():
            others = pair_predicates[(rel["src_id"], rel["dst_id"])]
            contested = any(contradicts(rel["predicate"], p) for p in others)
            result.append(
                NeighbourRelation(
                    relation_id=relation_id,
                    src_concept_id=rel["src_id"],
                    src_name=rel["src_name"],
                    predicate=rel["predicate"],
                    dst_concept_id=rel["dst_id"],
                    dst_name=rel["dst_name"],
                    strength=relation_strength(
                        rel["contribs"], now=now, half_life_days=half_life_days
                    ),
                    creator_count=distinct_creator_count(rel["contribs"]),
                    contested=contested,
                    evidence_claim_ids=sorted(rel["claim_ids"]),
                    evidence=[rel["evidence"][cid] for cid in sorted(rel["evidence"])],
                    **_attribution(anchor_id, subtree_set, rel["src_id"],
                                   rel["src_name"], rel["dst_id"], rel["dst_name"]),
                )
            )
        # Best-supported first; relation_id as a stable tiebreak.
        result.sort(key=lambda x: (-x.strength, x.relation_id))
        return result

    def set_creator_trust_tier(self, creator_id: int, tier: int) -> bool:
        """Set the owner's trust-tier on a Creator (ADR-0013 "Strength").

        Higher tiers weight that creator more in every relationship's Strength.
        Returns whether the Creator existed. Does not commit.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE creators SET trust_tier = %s WHERE id = %s", (tier, creator_id)
            )
            return cur.rowcount > 0

    # -- Hierarchy: the owner-curated `broader-of` taxonomy (ADR-0013) ----------
    #
    # `broader --broader-of--> narrower` edges, proposed (props.confirmed='false',
    # invisible to roll-up) until the owner confirms (props.confirmed='true'). The
    # DB cycle-guard trigger keeps the graph a DAG, so a proposal that would close a
    # loop raises rather than persisting.

    def propose_broader_of(self, broader_id: int, narrower_id: int) -> None:
        """Record a *proposed* `broader-of` edge — a suggestion, invisible to roll-up.

        Idempotent: re-proposing the same pair leaves the existing edge (and its
        confirmation state) untouched. The cycle-guard trigger rejects a proposal
        that would close a loop.
        """
        self.add_edge(
            "concept", broader_id, "concept", narrower_id, "broader-of",
            props={"confirmed": False},
        )

    def confirm_broader_of(self, broader_id: int, narrower_id: int) -> bool:
        """Confirm a proposed `broader-of` edge, making it visible to roll-up.

        Flips `props.confirmed` to true; the UPDATE re-checks the cycle guard, so a
        confirmation can never introduce a cycle. Returns whether the edge existed.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE edges SET props = jsonb_set(props, '{confirmed}', 'true') "
                "WHERE kind = 'broader-of' AND src_type = 'concept' AND src_id = %s "
                "AND dst_type = 'concept' AND dst_id = %s",
                (broader_id, narrower_id),
            )
            return cur.rowcount > 0

    def reject_broader_of(self, broader_id: int, narrower_id: int) -> bool:
        """Reject (delete) a proposed-or-confirmed `broader-of` edge. ``False`` if absent."""
        return self.remove_edge(
            "concept", broader_id, "concept", narrower_id, "broader-of"
        )

    def broader_parents(
        self, concept_id: int, *, confirmed_only: bool = False
    ) -> list[ConceptRef]:
        """The Concept's `broader-of` parents (its broader Concepts), ADR-0013.

        With `confirmed_only`, only confirmed parents — what roll-up actually
        traverses; otherwise proposed parents are included too (the curation view).
        """
        clause = ""
        if confirmed_only:
            clause = " AND COALESCE(e.props->>'confirmed', 'false') = 'true'"
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.name FROM edges e JOIN concepts c ON c.id = e.src_id "
                "WHERE e.kind = 'broader-of' AND e.dst_type = 'concept' "
                "AND e.dst_id = %s AND e.src_type = 'concept'" + clause + " "
                "ORDER BY c.name",
                (concept_id,),
            )
            return [ConceptRef(id=r[0], name=r[1]) for r in cur.fetchall()]

    def list_broader_of(self, *, confirmed: bool | None = None) -> list[tuple]:
        """Every `broader-of` edge as (broader_id, narrower_id, confirmed) — for the
        curation view and tests. `confirmed` filters to confirmed/proposed when set."""
        clause = ""
        if confirmed is not None:
            want = "true" if confirmed else "false"
            clause = f" AND COALESCE(props->>'confirmed', 'false') = '{want}'"
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT src_id, dst_id, "
                "       COALESCE(props->>'confirmed', 'false') = 'true' "
                "FROM edges WHERE kind = 'broader-of'" + clause + " "
                "ORDER BY src_id, dst_id",
            )
            return [(r[0], r[1], r[2]) for r in cur.fetchall()]

    def nearest_concept(
        self, embedding: list[float], *, model: str
    ) -> NearestConcept | None:
        """The closest existing Concept by cosine distance, within one model.

        Concept normalization compares embeddings only against same-model vectors
        (cross-model distances are meaningless, ADR-0008). Returns ``None`` when no
        Concept has been embedded yet — the first mention always mints a Concept.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.name, e.embedding <=> %s::vector AS distance "
                "FROM embeddings e JOIN concepts c ON c.id = e.owner_id "
                "WHERE e.owner_type = 'concept' AND e.model = %s "
                "ORDER BY e.embedding <=> %s::vector LIMIT 1",
                (_vector_literal(embedding), model, _vector_literal(embedding)),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return NearestConcept(concept_id=row[0], name=row[1], distance=float(row[2]))

    def nearest_concepts(
        self, embedding: list[float], *, model: str, limit: int, max_distance: float
    ) -> list[NearestConcept]:
        """The Concepts nearest a query embedding, closest first, within a cutoff.

        The pgvector half of grounded retrieval (ADR-0011): a free-text question is
        embedded and matched against the *same* Concept embeddings normalization
        and the Impact engine use (ADR-0008), so the three share one retrieval
        path. Concepts beyond `max_distance` are excluded, so a question the
        library does not cover retrieves nothing — and the answer abstains — rather
        than latching onto the merely least-distant Concept. Compared only within
        one embedding model, since cross-model distances are meaningless.
        """
        vec = _vector_literal(embedding)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.name, e.embedding <=> %s::vector AS distance "
                "FROM embeddings e JOIN concepts c ON c.id = e.owner_id "
                "WHERE e.owner_type = 'concept' AND e.model = %s "
                "  AND e.embedding <=> %s::vector <= %s "
                "ORDER BY e.embedding <=> %s::vector LIMIT %s",
                (vec, model, vec, max_distance, vec, limit),
            )
            return [
                NearestConcept(concept_id=r[0], name=r[1], distance=float(r[2]))
                for r in cur.fetchall()
            ]

    # -- grounded query retrieval (reads) ------------------------------------

    def retrieve_evidence(
        self, concept_ids: list[int], *, limit: int
    ) -> RetrievedEvidence:
        """Gather the evidence referencing any of `concept_ids` — the Concept-
        traversal half of grounded retrieval (issue #17, ADR-0011).

        Spans the Body of Knowledge (Claims, Protocols) and the personal layer
        (Goals, the latest Marker reading per Concept, Decisions), so an answer can
        be both cited and actionable. Claims, Protocols, Goals, and Decisions are
        ranked by how many of the query's Concepts they touch (most-overlapping
        first) and capped at `limit`; the latest-reading-per-Concept Markers are
        capped too. A Marker references its Concept by FK (`concept_id`), not an
        edge (ADR-0008), so it is matched directly. Returns empty lists for empty
        `concept_ids` — the caller's signal to abstain.
        """
        if not concept_ids:
            return RetrievedEvidence()
        ids = list(dict.fromkeys(concept_ids))
        return RetrievedEvidence(
            claims=self._evidence_claims(ids, limit),
            protocols=self._evidence_protocols(ids, limit),
            goals=self._evidence_goals(ids, limit),
            markers=self._evidence_markers(ids, limit),
            decisions=self._evidence_decisions(ids, limit),
        )

    def _evidence_claims(self, ids: list[int], limit: int) -> list[EvidenceClaim]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cl.id, cl.text, cl.type, cl.locator_seconds, v.url, v.title, "
                "  ARRAY(SELECT c.name FROM edges e2 JOIN concepts c ON c.id = e2.dst_id "
                "        WHERE e2.src_type = 'claim' AND e2.src_id = cl.id "
                "          AND e2.dst_type = 'concept' AND e2.kind = 'references' "
                "        ORDER BY c.name) "
                "FROM claims cl "
                "JOIN videos v ON v.video_id = cl.video_id "
                "JOIN edges e ON e.src_type = 'claim' AND e.src_id = cl.id "
                "  AND e.dst_type = 'concept' AND e.kind = 'references' "
                "  AND e.dst_id = ANY(%s) "
                "GROUP BY cl.id, v.url, v.title "
                "ORDER BY count(DISTINCT e.dst_id) DESC, cl.id "
                "LIMIT %s",
                (ids, limit),
            )
            return [
                EvidenceClaim(
                    id=r[0],
                    text=r[1],
                    type=r[2],
                    deep_link=locator_url(r[4], r[3]),
                    source_title=r[5],
                    concepts=list(r[6]),
                )
                for r in cur.fetchall()
            ]

    def _evidence_protocols(self, ids: list[int], limit: int) -> list[EvidenceProtocol]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT p.id, p.action, p.dose, p.timing, p.frequency, p.duration, "
                "  p.locator_seconds, v.url, v.title, "
                "  ARRAY(SELECT c.name FROM edges e2 JOIN concepts c ON c.id = e2.dst_id "
                "        WHERE e2.src_type = 'protocol' AND e2.src_id = p.id "
                "          AND e2.dst_type = 'concept' AND e2.kind = 'references' "
                "        ORDER BY c.name) "
                "FROM protocols p "
                "JOIN videos v ON v.video_id = p.video_id "
                "JOIN edges e ON e.src_type = 'protocol' AND e.src_id = p.id "
                "  AND e.dst_type = 'concept' AND e.kind = 'references' "
                "  AND e.dst_id = ANY(%s) "
                "GROUP BY p.id, v.url, v.title "
                "ORDER BY count(DISTINCT e.dst_id) DESC, p.id "
                "LIMIT %s",
                (ids, limit),
            )
            return [
                EvidenceProtocol(
                    id=r[0],
                    action=r[1],
                    dose=r[2],
                    timing=r[3],
                    frequency=r[4],
                    duration=r[5],
                    deep_link=locator_url(r[7], r[6]),
                    source_title=r[8],
                    concepts=list(r[9]),
                )
                for r in cur.fetchall()
            ]

    def _evidence_goals(self, ids: list[int], limit: int) -> list[EvidenceGoal]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT g.id, g.title, g.detail, "
                "  ARRAY(SELECT c.name FROM edges e2 JOIN concepts c ON c.id = e2.dst_id "
                "        WHERE e2.src_type = 'goal' AND e2.src_id = g.id "
                "          AND e2.dst_type = 'concept' AND e2.kind = 'references' "
                "        ORDER BY c.name) "
                "FROM goals g "
                "JOIN edges e ON e.src_type = 'goal' AND e.src_id = g.id "
                "  AND e.dst_type = 'concept' AND e.kind = 'references' "
                "  AND e.dst_id = ANY(%s) "
                "GROUP BY g.id "
                "ORDER BY count(DISTINCT e.dst_id) DESC, g.id "
                "LIMIT %s",
                (ids, limit),
            )
            return [
                EvidenceGoal(id=r[0], title=r[1], detail=r[2], concepts=list(r[3]))
                for r in cur.fetchall()
            ]

    def _evidence_markers(self, ids: list[int], limit: int) -> list[EvidenceMarker]:
        with self._conn.cursor() as cur:
            cur.execute(
                "WITH latest AS ("
                "  SELECT DISTINCT ON (concept_id) concept_id, value, unit, "
                "         reference_low, reference_high, measured_at "
                "  FROM markers WHERE concept_id = ANY(%s) "
                "  ORDER BY concept_id, measured_at DESC, id DESC) "
                "SELECT c.name, l.value, l.unit, l.reference_low, l.reference_high, "
                "       l.measured_at "
                "FROM latest l JOIN concepts c ON c.id = l.concept_id "
                "ORDER BY c.name LIMIT %s",
                (ids, limit),
            )
            return [
                EvidenceMarker(
                    concept=r[0],
                    value=float(r[1]),
                    unit=r[2],
                    reference_low=float(r[3]) if r[3] is not None else None,
                    reference_high=float(r[4]) if r[4] is not None else None,
                    measured_at=r[5],
                )
                for r in cur.fetchall()
            ]

    def _evidence_decisions(self, ids: list[int], limit: int) -> list[EvidenceDecision]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT d.id, d.action, d.dose, d.timing, d.frequency, d.duration, "
                "  d.note, "
                "  ARRAY(SELECT c.name FROM edges e2 JOIN concepts c ON c.id = e2.dst_id "
                "        WHERE e2.src_type = 'decision' AND e2.src_id = d.id "
                "          AND e2.dst_type = 'concept' AND e2.kind = 'references' "
                "        ORDER BY c.name) "
                "FROM decisions d "
                "JOIN edges e ON e.src_type = 'decision' AND e.src_id = d.id "
                "  AND e.dst_type = 'concept' AND e.kind = 'references' "
                "  AND e.dst_id = ANY(%s) "
                "GROUP BY d.id "
                "ORDER BY count(DISTINCT e.dst_id) DESC, d.id "
                "LIMIT %s",
                (ids, limit),
            )
            return [
                EvidenceDecision(
                    id=r[0],
                    action=r[1],
                    dose=r[2],
                    timing=r[3],
                    frequency=r[4],
                    duration=r[5],
                    note=r[6],
                    concepts=list(r[7]),
                )
                for r in cur.fetchall()
            ]

    # -- Body of Knowledge (reads) -------------------------------------------

    def admitted_claims(self, video_id: str) -> list[AdmittedClaim]:
        """A video's admitted Claims, each with its locator deep-link (ADR-0010)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cl.id, cl.text, cl.type, cl.locator_seconds, v.url, "
                "  ARRAY(SELECT c.name FROM edges e JOIN concepts c ON c.id = e.dst_id "
                "        WHERE e.src_type = 'claim' AND e.src_id = cl.id "
                "          AND e.dst_type = 'concept' AND e.kind = 'references' "
                "        ORDER BY c.name) "
                "FROM claims cl JOIN videos v ON v.video_id = cl.video_id "
                "WHERE cl.video_id = %s ORDER BY cl.locator_seconds, cl.id",
                (video_id,),
            )
            return [
                AdmittedClaim(
                    id=r[0],
                    text=r[1],
                    type=r[2],
                    locator_seconds=r[3],
                    deep_link=locator_url(r[4], r[3]),
                    concepts=list(r[5]),
                )
                for r in cur.fetchall()
            ]

    def admitted_protocols(self, video_id: str) -> list[AdmittedProtocol]:
        """A video's admitted Protocols, each with its locator deep-link (ADR-0010)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT p.id, p.action, p.dose, p.timing, p.frequency, p.duration, "
                "  p.locator_seconds, v.url, "
                "  ARRAY(SELECT c.name FROM edges e JOIN concepts c ON c.id = e.dst_id "
                "        WHERE e.src_type = 'protocol' AND e.src_id = p.id "
                "          AND e.dst_type = 'concept' AND e.kind = 'references' "
                "        ORDER BY c.name) "
                "FROM protocols p JOIN videos v ON v.video_id = p.video_id "
                "WHERE p.video_id = %s ORDER BY p.locator_seconds, p.id",
                (video_id,),
            )
            return [
                AdmittedProtocol(
                    id=r[0],
                    action=r[1],
                    dose=r[2],
                    timing=r[3],
                    frequency=r[4],
                    duration=r[5],
                    locator_seconds=r[6],
                    deep_link=locator_url(r[7], r[6]),
                    concepts=list(r[8]),
                )
                for r in cur.fetchall()
            ]

    # == Body of Knowledge: browse, detail & in-place curation (issue #14) ====
    #
    # The browsable, editable evidence layer (ADR-0009, ADR-0010). Reads list and
    # open Claims/Protocols/Concepts and resolve their connections over `edges`;
    # writes edit a Claim/Protocol in place — flagging it a protected version so
    # re-extraction won't clobber it (ADR-0005) — or delete it and the edges that
    # hang off it. As elsewhere, these do not commit: the caller owns the boundary.

    # -- browse & detail (reads) ---------------------------------------------

    def list_claims(
        self, *, concept_id: int | None = None, type: str | None = None
    ) -> list[BokClaim]:
        """Every admitted Claim for the BoK browser, newest first; filterable.

        Optionally narrowed to Claims referencing a given Concept and/or of a
        given sub-kind, so the owner can slice the evidence layer while browsing
        (issue #14). Each Claim carries its provenance, locator deep-link, and
        referenced Concepts; `supports` is left for the detail read.
        """
        where: list[str] = []
        params: dict = {}
        if concept_id is not None:
            where.append(
                "EXISTS (SELECT 1 FROM edges e2 WHERE e2.src_type = 'claim' "
                "AND e2.src_id = cl.id AND e2.dst_type = 'concept' "
                "AND e2.dst_id = %(concept_id)s AND e2.kind = 'references')"
            )
            params["concept_id"] = concept_id
        if type is not None:
            where.append("cl.type = %(type)s")
            params["type"] = type
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._conn.cursor() as cur:
            cur.execute(
                _CLAIM_SELECT + clause + " ORDER BY cl.created_at DESC, cl.id DESC",
                params,
            )
            return [_row_to_bok_claim(r) for r in cur.fetchall()]

    def get_claim(self, claim_id: int) -> BokClaim | None:
        """One admitted Claim with its connections, or ``None`` if it's gone.

        Fills `supports` — the Protocols this Claim justifies (`claim → protocol
        supports`, ADR-0008) — so the detail view can link to them. The referenced
        Concepts come back on the base read.
        """
        with self._conn.cursor() as cur:
            cur.execute(_CLAIM_SELECT + " WHERE cl.id = %s", (claim_id,))
            row = cur.fetchone()
            if row is None:
                return None
            claim = _row_to_bok_claim(row)
            cur.execute(
                "SELECT p.id, p.action FROM edges e JOIN protocols p ON p.id = e.dst_id "
                "WHERE e.src_type = 'claim' AND e.src_id = %s "
                "AND e.dst_type = 'protocol' AND e.kind = 'supports' "
                "ORDER BY p.action, p.id",
                (claim_id,),
            )
            supports = [ProtocolRef(id=r[0], action=r[1]) for r in cur.fetchall()]
        return replace(claim, supports=supports)

    def list_protocols(self, *, concept_id: int | None = None) -> list[BokProtocol]:
        """Every admitted Protocol for the BoK browser, newest first; filterable
        by referenced Concept. Each carries its structured parameters, provenance,
        locator deep-link, and Concepts; `justified_by` is left for the detail read.
        """
        where = ""
        params: dict = {}
        if concept_id is not None:
            where = (
                " WHERE EXISTS (SELECT 1 FROM edges e2 WHERE e2.src_type = 'protocol' "
                "AND e2.src_id = p.id AND e2.dst_type = 'concept' "
                "AND e2.dst_id = %(concept_id)s AND e2.kind = 'references')"
            )
            params["concept_id"] = concept_id
        with self._conn.cursor() as cur:
            cur.execute(
                _PROTOCOL_SELECT + where + " ORDER BY p.created_at DESC, p.id DESC",
                params,
            )
            return [_row_to_bok_protocol(r) for r in cur.fetchall()]

    def get_protocol(self, protocol_id: int) -> BokProtocol | None:
        """One admitted Protocol with its connections, or ``None`` if it's gone.

        Fills `justified_by` — the Claims that support it — so the detail view can
        show and link to the evidence behind the recommendation (CONTEXT.md
        "Protocol"; ADR-0008).
        """
        with self._conn.cursor() as cur:
            cur.execute(_PROTOCOL_SELECT + " WHERE p.id = %s", (protocol_id,))
            row = cur.fetchone()
            if row is None:
                return None
            protocol = _row_to_bok_protocol(row)
            cur.execute(
                "SELECT cl.id, cl.text FROM edges e JOIN claims cl ON cl.id = e.src_id "
                "WHERE e.dst_type = 'protocol' AND e.dst_id = %s "
                "AND e.src_type = 'claim' AND e.kind = 'supports' "
                "ORDER BY cl.id",
                (protocol_id,),
            )
            justified_by = [ClaimRef(id=r[0], text=r[1]) for r in cur.fetchall()]
        return replace(protocol, justified_by=justified_by)

    def list_concepts(self, *, kind: str | None = None) -> list[BokConcept]:
        """Every Concept hub node, alphabetical; optionally filtered by kind.

        Each carries a `reference_count` of the Claims + Protocols that reference
        it, so the browser can show how load-bearing a Concept is at a glance.
        """
        where = ""
        params: dict = {}
        if kind is not None:
            where = " WHERE c.kind = %(kind)s"
            params["kind"] = kind
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.name, c.kind, "
                "  (SELECT count(*) FROM edges e WHERE e.dst_type = 'concept' "
                "   AND e.dst_id = c.id AND e.kind = 'references') "
                "FROM concepts c" + where + " ORDER BY c.name, c.id",
                params,
            )
            return [
                BokConcept(id=r[0], name=r[1], kind=r[2], reference_count=r[3])
                for r in cur.fetchall()
            ]

    def get_concept(self, concept_id: int) -> BokConcept | None:
        """One Concept with everything that references it, or ``None`` if gone.

        Fills `claims` and `protocols` by walking the inbound `references` edges,
        so the owner can pivot from a Concept to all the evidence touching it — the
        relatedness-by-shared-Concept traversal, without a visual graph (ADR-0009).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, kind FROM concepts WHERE id = %s", (concept_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT cl.id, cl.text FROM edges e JOIN claims cl ON cl.id = e.src_id "
                "WHERE e.dst_type = 'concept' AND e.dst_id = %s "
                "AND e.src_type = 'claim' AND e.kind = 'references' "
                "ORDER BY cl.id",
                (concept_id,),
            )
            claims = [ClaimRef(id=r[0], text=r[1]) for r in cur.fetchall()]
            cur.execute(
                "SELECT p.id, p.action FROM edges e JOIN protocols p ON p.id = e.src_id "
                "WHERE e.dst_type = 'concept' AND e.dst_id = %s "
                "AND e.src_type = 'protocol' AND e.kind = 'references' "
                "ORDER BY p.action, p.id",
                (concept_id,),
            )
            protocols = [ProtocolRef(id=r[0], action=r[1]) for r in cur.fetchall()]
        return BokConcept(
            id=row[0],
            name=row[1],
            kind=row[2],
            reference_count=len(claims) + len(protocols),
            claims=claims,
            protocols=protocols,
        )

    # -- in-place edit & delete (writes) -------------------------------------

    def update_claim(
        self, claim_id: int, *, text: str, type: str, locator_seconds: int
    ) -> bool:
        """Apply an owner edit to a Claim and mark it a protected version (ADR-0010).

        Returns whether the Claim existed. Setting `protected` *here* makes the
        invariant unbreakable: every in-place edit flows through this one write, so
        a later re-extraction supersede (ADR-0005) can trust the flag and never
        silently clobber a hand-corrected Claim.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET text = %s, type = %s, locator_seconds = %s, "
                "protected = TRUE WHERE id = %s",
                (text, type, locator_seconds, claim_id),
            )
            return cur.rowcount > 0

    def update_protocol(
        self,
        protocol_id: int,
        *,
        action: str,
        dose: str | None,
        timing: str | None,
        frequency: str | None,
        duration: str | None,
        locator_seconds: int,
    ) -> bool:
        """Apply an owner edit to a Protocol and mark it protected (ADR-0010).

        Returns whether the Protocol existed. The DB still enforces the structure
        CHECK (at least one of dose/timing/frequency/duration), so an edit that
        strips a Protocol bare fails loudly rather than admitting vague advice.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE protocols SET action = %s, dose = %s, timing = %s, "
                "frequency = %s, duration = %s, locator_seconds = %s, "
                "protected = TRUE WHERE id = %s",
                (action, dose, timing, frequency, duration, locator_seconds, protocol_id),
            )
            return cur.rowcount > 0

    def delete_claim(self, claim_id: int) -> bool:
        """Delete a Claim and the edges/embeddings hanging off it (issue #14).

        `edges` endpoints are polymorphic, not FKs, so there is no cascade, and the
        integrity trigger only guards INSERT/UPDATE — a delete must clear the
        Claim's own edges (as either endpoint) itself or they would dangle. Returns
        whether the Claim existed.
        """
        # Capture the lateral relationships this Claim evidences *before* it goes:
        # deleting the Claim cascades its evidence links away, after which any
        # relationship left unevidenced is self-healed (ADR-0013).
        evidenced = self.relations_evidenced_by(claim_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edges WHERE (src_type = 'claim' AND src_id = %(id)s) "
                "OR (dst_type = 'claim' AND dst_id = %(id)s)",
                {"id": claim_id},
            )
            cur.execute(
                "DELETE FROM embeddings WHERE owner_type = 'claim' AND owner_id = %s",
                (claim_id,),
            )
            # A Claim is only ever an Impact's source; clear any so none dangle.
            cur.execute(
                "DELETE FROM impacts WHERE source_type = 'claim' AND source_id = %s",
                (claim_id,),
            )
            cur.execute("DELETE FROM claims WHERE id = %s", (claim_id,))
            existed = cur.rowcount > 0
        # Remove relationships whose last evidencing Claim was this one.
        self.prune_orphaned_relations(evidenced)
        return existed

    def delete_protocol(self, protocol_id: int) -> bool:
        """Delete a Protocol and the edges/embeddings hanging off it (issue #14).

        Clears both its outbound `references` edges to Concepts and the inbound
        `supports` edges from Claims, so no dangling edge survives the delete.
        Returns whether the Protocol existed.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edges WHERE (src_type = 'protocol' AND src_id = %(id)s) "
                "OR (dst_type = 'protocol' AND dst_id = %(id)s)",
                {"id": protocol_id},
            )
            cur.execute(
                "DELETE FROM embeddings WHERE owner_type = 'protocol' AND owner_id = %s",
                (protocol_id,),
            )
            # A Protocol is only ever an Impact's source; clear any so none dangle.
            cur.execute(
                "DELETE FROM impacts WHERE source_type = 'protocol' AND source_id = %s",
                (protocol_id,),
            )
            cur.execute("DELETE FROM protocols WHERE id = %s", (protocol_id,))
            return cur.rowcount > 0

    # == Personal layer: Goals, Markers, Decisions & linking (issue #16) ======
    #
    # The owner-specific layer (CONTEXT.md "Personal Layer"). Writes record a Goal,
    # a Marker reading, or a Decision and assert its Concept-`references` edges;
    # reads list and open each with its connections resolved over `edges`; the
    # suggester finds Protocols/Claims/Goals sharing a Concept with a Decision so the
    # owner can confirm a link. As elsewhere these do not commit — the caller (the
    # `personal` service) owns the transaction boundary.

    # -- Goals ---------------------------------------------------------------

    def add_goal(self, *, title: str, detail: str | None = None) -> int:
        """Persist a Goal — a stable intention or risk (CONTEXT.md "Goal")."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO goals (title, detail) VALUES (%s, %s) RETURNING id",
                (title, detail),
            )
            return cur.fetchone()[0]

    def list_goals(self) -> list[Goal]:
        """Every Goal, newest first, each with the Concepts it concerns and the
        Decisions that serve it — so an *unmet* Goal (empty `served_by`) is visible.
        """
        with self._conn.cursor() as cur:
            cur.execute(_GOAL_SELECT + " ORDER BY g.created_at DESC, g.id DESC")
            return [_row_to_goal(r) for r in cur.fetchall()]

    def get_goal(self, goal_id: int) -> Goal | None:
        """One Goal with its Concepts and serving Decisions, or ``None`` if gone."""
        with self._conn.cursor() as cur:
            cur.execute(_GOAL_SELECT + " WHERE g.id = %s", (goal_id,))
            row = cur.fetchone()
        return _row_to_goal(row) if row is not None else None

    def delete_goal(self, goal_id: int) -> bool:
        """Delete a Goal and the edges hanging off it (its Concept `references` and
        any inbound `decision -> goal serves`), so none dangle. ``False`` if gone.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edges WHERE (src_type = 'goal' AND src_id = %(id)s) "
                "OR (dst_type = 'goal' AND dst_id = %(id)s)",
                {"id": goal_id},
            )
            # A Goal is only ever an Impact's anchor; clear any so none dangle.
            cur.execute(
                "DELETE FROM impacts WHERE anchor_type = 'goal' AND anchor_id = %s",
                (goal_id,),
            )
            cur.execute("DELETE FROM goals WHERE id = %s", (goal_id,))
            return cur.rowcount > 0

    # -- Markers (append-only time-series) -----------------------------------

    def add_marker_reading(
        self,
        *,
        concept_id: int,
        value: float,
        unit: str,
        reference_low: float | None,
        reference_high: float | None,
        measured_at: datetime,
    ) -> int:
        """Append one dated Marker reading (CONTEXT.md "Marker").

        Always an INSERT — never an update: the database trigger blocks any mutate,
        so a reading is an immutable snapshot and a correction is a *new* reading.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO markers (concept_id, value, unit, reference_low, "
                "reference_high, measured_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (concept_id, value, unit, reference_low, reference_high, measured_at),
            )
            return cur.fetchone()[0]

    def list_marker_series(self) -> list[MarkerSeries]:
        """One series per referenced Concept: its latest reading + reading count.

        The Web App's Marker overview — each Concept the owner tracks, its most
        recent value (with derived out-of-range) and how deep the history runs.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "WITH latest AS ("
                "  SELECT DISTINCT ON (concept_id) id, concept_id, value, unit, "
                "         reference_low, reference_high, measured_at "
                "  FROM markers ORDER BY concept_id, measured_at DESC, id DESC) "
                "SELECT l.id, l.concept_id, c.name, l.value, l.unit, "
                "       l.reference_low, l.reference_high, l.measured_at, "
                "       (SELECT count(*) FROM markers m2 WHERE m2.concept_id = l.concept_id) "
                "FROM latest l JOIN concepts c ON c.id = l.concept_id "
                "ORDER BY c.name, l.concept_id"
            )
            return [
                MarkerSeries(
                    concept=ConceptRef(id=r[1], name=r[2]),
                    unit=r[4],
                    reading_count=r[8],
                    latest=_row_to_marker_reading(r[:8]),
                )
                for r in cur.fetchall()
            ]

    def marker_history(self, concept_id: int) -> list[MarkerReading]:
        """A Concept's whole Marker history as a series, newest reading first."""
        with self._conn.cursor() as cur:
            cur.execute(
                _MARKER_READING_SELECT + " WHERE m.concept_id = %s "
                "ORDER BY m.measured_at DESC, m.id DESC",
                (concept_id,),
            )
            return [_row_to_marker_reading(r) for r in cur.fetchall()]

    def list_marker_readings(self) -> list[MarkerReading]:
        """Every Marker reading, newest first — the picker for a Decision's
        `motivated_by` link (which reading prompted the Decision).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                _MARKER_READING_SELECT + " ORDER BY m.measured_at DESC, m.id DESC"
            )
            return [_row_to_marker_reading(r) for r in cur.fetchall()]

    # -- Decisions -----------------------------------------------------------

    def add_decision(
        self,
        *,
        action: str,
        dose: str | None,
        timing: str | None,
        frequency: str | None,
        duration: str | None,
        started_at: datetime,
        ended_at: datetime | None,
        note: str | None,
    ) -> int:
        """Persist a Decision with its own actual parameters (CONTEXT.md "Decision")."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO decisions (action, dose, timing, frequency, duration, "
                "started_at, ended_at, note) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (action, dose, timing, frequency, duration, started_at, ended_at, note),
            )
            return cur.fetchone()[0]

    def list_decisions(self) -> list[Decision]:
        """Every Decision, newest started first, each with the Concepts it
        references; the rest of a Decision's connections come on the detail read.
        """
        with self._conn.cursor() as cur:
            cur.execute(_DECISION_SELECT + " ORDER BY d.started_at DESC, d.id DESC")
            return [_row_to_decision(r) for r in cur.fetchall()]

    def get_decision(self, decision_id: int) -> Decision | None:
        """One Decision with its full rationale, or ``None`` if it's gone.

        Fills every connection by traversing `edges` both ways: the Protocol(s) it
        `implements`, the Goal(s) it `serves`, the Marker(s) that `motivated_by` it,
        and the Claim(s) that `support` it — so the owner can review the supporting
        evidence and the Goals served from one place (issue #16).
        """
        with self._conn.cursor() as cur:
            cur.execute(_DECISION_SELECT + " WHERE d.id = %s", (decision_id,))
            row = cur.fetchone()
            if row is None:
                return None
            decision = _row_to_decision(row)
            cur.execute(
                "SELECT p.id, p.action FROM edges e "
                "JOIN protocols p ON p.id = e.dst_id "
                "WHERE e.src_type = 'decision' AND e.src_id = %s "
                "AND e.dst_type = 'protocol' AND e.kind = 'implements' "
                "ORDER BY p.action, p.id",
                (decision_id,),
            )
            implements = [ProtocolRef(id=r[0], action=r[1]) for r in cur.fetchall()]
            cur.execute(
                "SELECT g.id, g.title FROM edges e JOIN goals g ON g.id = e.dst_id "
                "WHERE e.src_type = 'decision' AND e.src_id = %s "
                "AND e.dst_type = 'goal' AND e.kind = 'serves' "
                "ORDER BY g.title, g.id",
                (decision_id,),
            )
            serves = [GoalRef(id=r[0], title=r[1]) for r in cur.fetchall()]
            cur.execute(
                "SELECT m.id, c.name, m.value, m.unit, m.measured_at FROM edges e "
                "JOIN markers m ON m.id = e.dst_id "
                "JOIN concepts c ON c.id = m.concept_id "
                "WHERE e.src_type = 'decision' AND e.src_id = %s "
                "AND e.dst_type = 'marker' AND e.kind = 'motivated_by' "
                "ORDER BY m.measured_at DESC, m.id DESC",
                (decision_id,),
            )
            motivated_by = [
                MarkerRef(id=r[0], concept=r[1], value=float(r[2]), unit=r[3],
                          measured_at=r[4])
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT cl.id, cl.text FROM edges e JOIN claims cl ON cl.id = e.src_id "
                "WHERE e.dst_type = 'decision' AND e.dst_id = %s "
                "AND e.src_type = 'claim' AND e.kind = 'supports' "
                "ORDER BY cl.id",
                (decision_id,),
            )
            supported_by = [ClaimRef(id=r[0], text=r[1]) for r in cur.fetchall()]
        return replace(
            decision,
            implements=implements,
            serves=serves,
            motivated_by=motivated_by,
            supported_by=supported_by,
        )

    def delete_decision(self, decision_id: int) -> bool:
        """Delete a Decision and every edge hanging off it (Concept `references`,
        `implements`/`serves`/`motivated_by` it owns, and inbound `claim supports`),
        so none dangle. ``False`` if it's already gone.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edges WHERE (src_type = 'decision' AND src_id = %(id)s) "
                "OR (dst_type = 'decision' AND dst_id = %(id)s)",
                {"id": decision_id},
            )
            # A Decision is only ever an Impact's anchor; clear any so none dangle.
            # An Impact this Decision *actioned* keeps its row (its FK is ON DELETE
            # SET NULL), so the audit trail of what the owner saw survives.
            cur.execute(
                "DELETE FROM impacts WHERE anchor_type = 'decision' AND anchor_id = %s",
                (decision_id,),
            )
            cur.execute("DELETE FROM decisions WHERE id = %s", (decision_id,))
            return cur.rowcount > 0

    # -- suggest-then-confirm linking ----------------------------------------

    def concept_ids_for(self, src_type: str, src_id: int) -> list[int]:
        """The Concept ids a Claim/Protocol/Goal/Decision references.

        Used to seed a Decision's Concepts from the Protocol it adopts, so the
        suggester has overlap to work with the moment the Decision is recorded.
        `src_type` is a caller-controlled literal, never user input.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT dst_id FROM edges WHERE src_type = %s AND src_id = %s "
                "AND dst_type = 'concept' AND kind = 'references' ORDER BY dst_id",
                (src_type, src_id),
            )
            return [r[0] for r in cur.fetchall()]

    def decision_link_suggestions(self, decision_id: int) -> list[SuggestedLink]:
        """Protocols, Claims, and Goals that share a Concept with the Decision.

        The suggest half of suggest-then-confirm (issue #16): an entity is relevant
        when it `references` a Concept the Decision also references — the Concept
        overlap built on the normalized Concepts from Slice 8. Anything already
        linked to this Decision is excluded, so confirming a suggestion removes it
        from the next round. Ordered most-overlapping first within each kind.
        """
        suggestions: list[SuggestedLink] = []
        with self._conn.cursor() as cur:
            for node_type, table, label_col, exclude in (
                (
                    "protocol", "protocols", "action",
                    "NOT EXISTS (SELECT 1 FROM edges x WHERE x.src_type = 'decision' "
                    "AND x.src_id = %(d)s AND x.dst_type = 'protocol' "
                    "AND x.dst_id = n.id AND x.kind = 'implements')",
                ),
                (
                    "claim", "claims", "text",
                    "NOT EXISTS (SELECT 1 FROM edges x WHERE x.src_type = 'claim' "
                    "AND x.src_id = n.id AND x.dst_type = 'decision' "
                    "AND x.dst_id = %(d)s AND x.kind = 'supports')",
                ),
                (
                    "goal", "goals", "title",
                    "NOT EXISTS (SELECT 1 FROM edges x WHERE x.src_type = 'decision' "
                    "AND x.src_id = %(d)s AND x.dst_type = 'goal' "
                    "AND x.dst_id = n.id AND x.kind = 'serves')",
                ),
            ):
                cur.execute(
                    "WITH dc AS (SELECT dst_id FROM edges WHERE src_type = 'decision' "
                    "AND src_id = %(d)s AND dst_type = 'concept' AND kind = 'references') "
                    f"SELECT n.id, n.{label_col}, "
                    "array_agg(DISTINCT c.name ORDER BY c.name) "
                    f"FROM edges e JOIN dc ON dc.dst_id = e.dst_id "
                    f"JOIN {table} n ON n.id = e.src_id "
                    "JOIN concepts c ON c.id = e.dst_id "
                    f"WHERE e.src_type = %(t)s AND e.dst_type = 'concept' "
                    f"AND e.kind = 'references' AND {exclude} "
                    f"GROUP BY n.id, n.{label_col} "
                    "ORDER BY count(*) DESC, 2",
                    {"d": decision_id, "t": node_type},
                )
                suggestions.extend(
                    SuggestedLink(
                        target_type=node_type,
                        target_id=r[0],
                        label=r[1],
                        shared_concepts=list(r[2]),
                    )
                    for r in cur.fetchall()
                )
        return suggestions

    def remove_edge(
        self, src_type: str, src_id: int, dst_type: str, dst_id: int, kind: str
    ) -> bool:
        """Drop one edge, e.g. when the owner detaches a confirmed link. Returns
        whether an edge was removed.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edges WHERE src_type = %s AND src_id = %s "
                "AND dst_type = %s AND dst_id = %s AND kind = %s",
                (src_type, src_id, dst_type, dst_id, kind),
            )
            return cur.rowcount > 0

    # == Impact engine: candidates, persistence, inbox & lifecycle (issue #18) =
    #
    # Change detection reuses the Concept-traversal machinery query is built on
    # (ADR-0008, ADR-0011): candidate pairs share a Concept; the `StanceJudge` (in
    # the `impacts` service) then weighs each. These reads gather candidates and load
    # the subject's rendering for the judge; the writes persist a deduped Impact and
    # drive its lifecycle. As elsewhere the writes don't commit — the caller owns the
    # transaction.

    # -- candidate generation (reads) ----------------------------------------

    def load_impact_knowledge(
        self, source_type: str, source_id: int
    ) -> ImpactKnowledge | None:
        """Load a Claim/Protocol as the `knowledge` end of a judgement (issue #18).

        Used for the *forward* pass — a newly-admitted Claim/Protocol is the subject
        weighed against each candidate anchor — so it carries the entity's full
        referenced Concepts and a one-line rendering. ``None`` if it's gone.
        """
        with self._conn.cursor() as cur:
            if source_type == "claim":
                cur.execute(
                    "SELECT cl.text, " + _concept_refs_sql("claim", "cl.id") + " "
                    "FROM claims cl WHERE cl.id = %s",
                    (source_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                text = row[0]
                concepts = [c["name"] for c in row[1]]
            elif source_type == "protocol":
                cur.execute(
                    "SELECT p.action, p.dose, p.timing, p.frequency, p.duration, "
                    + _concept_refs_sql("protocol", "p.id") + " "
                    "FROM protocols p WHERE p.id = %s",
                    (source_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                text = row[0] + _params_text(row[1], row[2], row[3], row[4])
                concepts = [c["name"] for c in row[5]]
            else:
                raise ValueError(f"impact knowledge cannot be a {source_type!r}")
        return ImpactKnowledge(
            type=source_type, id=source_id, text=text, concepts=concepts
        )

    def load_impact_anchor(
        self, anchor_type: str, anchor_id: int
    ) -> ImpactAnchor | None:
        """Load a Decision/Goal as the `anchor` end of a judgement (issue #18).

        Used for the *reverse* pass — a newly-recorded Decision/Goal is the subject
        scanned against the existing Body of Knowledge — so it carries its full
        referenced Concepts and a one-line rendering. ``None`` if it's gone. (A
        Marker is never the reverse subject — recording a reading doesn't trigger a
        scan — so only Decision/Goal are loaded here.)
        """
        with self._conn.cursor() as cur:
            if anchor_type == "decision":
                cur.execute(
                    "SELECT d.action, d.dose, d.timing, d.frequency, d.duration, "
                    + _concept_refs_sql("decision", "d.id") + " "
                    "FROM decisions d WHERE d.id = %s",
                    (anchor_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                text = row[0] + _params_text(row[1], row[2], row[3], row[4])
                concepts = [c["name"] for c in row[5]]
            elif anchor_type == "goal":
                cur.execute(
                    "SELECT g.title, g.detail, " + _concept_refs_sql("goal", "g.id") + " "
                    "FROM goals g WHERE g.id = %s",
                    (anchor_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                text = row[0] + (f": {row[1]}" if row[1] else "")
                concepts = [c["name"] for c in row[2]]
            else:
                raise ValueError(f"impact anchor cannot be a {anchor_type!r} here")
        return ImpactAnchor(type=anchor_type, id=anchor_id, text=text, concepts=concepts)

    def impact_anchor_candidates(
        self, source_type: str, source_id: int, *, limit: int
    ) -> list[ImpactAnchor]:
        """Anchors sharing a Concept with a Claim/Protocol — the *forward* candidates.

        The Decisions, Goals, and latest-per-Concept Marker readings that reference a
        Concept the source also references (issue #18): the shared-Concept overlap
        the `StanceJudge` then weighs. Each anchor carries the overlapping Concept
        names. Capped per category at `limit` so a burst can't unbounded the judge.
        """
        concept_ids = self.concept_ids_for(source_type, source_id)
        if not concept_ids:
            return []
        anchors: list[ImpactAnchor] = []
        with self._conn.cursor() as cur:
            for node_type, table, label_col in (
                ("decision", "decisions", "action"),
                ("goal", "goals", "title"),
            ):
                cur.execute(
                    f"SELECT n.id, n.{label_col}, "
                    "array_agg(DISTINCT c.name ORDER BY c.name) "
                    f"FROM edges e JOIN {table} n ON n.id = e.src_id "
                    "JOIN concepts c ON c.id = e.dst_id "
                    f"WHERE e.src_type = %s AND e.dst_type = 'concept' "
                    "AND e.kind = 'references' AND e.dst_id = ANY(%s) "
                    f"GROUP BY n.id, n.{label_col} "
                    "ORDER BY count(DISTINCT e.dst_id) DESC, n.id LIMIT %s",
                    (node_type, concept_ids, limit),
                )
                anchors.extend(
                    ImpactAnchor(
                        type=node_type, id=r[0], text=r[1], concepts=list(r[2])
                    )
                    for r in cur.fetchall()
                )
            # Markers reference their Concept by FK, not an edge (ADR-0008): match
            # the latest reading per overlapping Concept directly.
            cur.execute(
                "WITH latest AS ("
                "  SELECT DISTINCT ON (concept_id) id, concept_id, value, unit, "
                "         reference_low, reference_high "
                "  FROM markers WHERE concept_id = ANY(%s) "
                "  ORDER BY concept_id, measured_at DESC, id DESC) "
                "SELECT l.id, c.name, l.value, l.unit, l.reference_low, "
                "       l.reference_high "
                "FROM latest l JOIN concepts c ON c.id = l.concept_id "
                "ORDER BY c.name LIMIT %s",
                (concept_ids, limit),
            )
            anchors.extend(
                ImpactAnchor(
                    type="marker",
                    id=r[0],
                    text=_marker_label(r[1], r[2], r[3], r[4], r[5]),
                    concepts=[r[1]],
                )
                for r in cur.fetchall()
            )
        return anchors

    def impact_knowledge_candidates(
        self, anchor_type: str, anchor_id: int, *, limit: int
    ) -> list[ImpactKnowledge]:
        """Claims/Protocols sharing a Concept with a Decision/Goal — *reverse* candidates.

        The Body-of-Knowledge entities a newly-recorded anchor should be scanned
        against (issue #18): those referencing a Concept the anchor also references.
        Each carries the overlapping Concept names; capped per category at `limit`.
        """
        concept_ids = self.concept_ids_for(anchor_type, anchor_id)
        if not concept_ids:
            return []
        knowledge: list[ImpactKnowledge] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cl.id, cl.text, "
                "array_agg(DISTINCT c.name ORDER BY c.name) "
                "FROM edges e JOIN claims cl ON cl.id = e.src_id "
                "JOIN concepts c ON c.id = e.dst_id "
                "WHERE e.src_type = 'claim' AND e.dst_type = 'concept' "
                "AND e.kind = 'references' AND e.dst_id = ANY(%s) "
                "GROUP BY cl.id ORDER BY count(DISTINCT e.dst_id) DESC, cl.id LIMIT %s",
                (concept_ids, limit),
            )
            knowledge.extend(
                ImpactKnowledge(type="claim", id=r[0], text=r[1], concepts=list(r[2]))
                for r in cur.fetchall()
            )
            cur.execute(
                "SELECT p.id, p.action, p.dose, p.timing, p.frequency, p.duration, "
                "array_agg(DISTINCT c.name ORDER BY c.name) "
                "FROM edges e JOIN protocols p ON p.id = e.src_id "
                "JOIN concepts c ON c.id = e.dst_id "
                "WHERE e.src_type = 'protocol' AND e.dst_type = 'concept' "
                "AND e.kind = 'references' AND e.dst_id = ANY(%s) "
                "GROUP BY p.id ORDER BY count(DISTINCT e.dst_id) DESC, p.id LIMIT %s",
                (concept_ids, limit),
            )
            knowledge.extend(
                ImpactKnowledge(
                    type="protocol",
                    id=r[0],
                    text=r[1] + _params_text(r[2], r[3], r[4], r[5]),
                    concepts=list(r[6]),
                )
                for r in cur.fetchall()
            )
        return knowledge

    def decisions_supported_by_claim(self, claim_id: int) -> list[int]:
        """The Decisions a Claim `supports` (ADR-0008) — the supersede anchors.

        When a re-extraction can no longer match a superseded Claim, the Impact
        engine raises an Impact against each Decision it supported (ADR-0005), so
        changed evidence under a Decision is surfaced, not silently broken.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT dst_id FROM edges WHERE src_type = 'claim' AND src_id = %s "
                "AND dst_type = 'decision' AND kind = 'supports' ORDER BY dst_id",
                (claim_id,),
            )
            return [r[0] for r in cur.fetchall()]

    # -- relationship alerting candidates (ADR-0013) -------------------------

    def anchors_tracking_relation(
        self, src_concept_id: int, dst_concept_id: int
    ) -> list[tuple[str, int]]:
        """Goals/Decisions a relationship is relevant to — Tier-1 targets (ADR-0013).

        A Goal/Decision tracks a relationship when it references a Concept that is an
        *ancestor-or-self* of either endpoint — so a development on a descendant
        ("Brain metabolism") reaches a Goal tracking the broader "Brain" (user story
        31). Returns distinct (anchor_type, anchor_id), Goals and Decisions only.
        """
        relevant = set(self.ancestor_concept_ids(src_concept_id))
        relevant |= set(self.ancestor_concept_ids(dst_concept_id))
        return self._anchors_referencing(relevant)

    def anchors_tracking_concept(self, concept_id: int) -> list[tuple[str, int]]:
        """Goals/Decisions that track a Concept via ancestor-or-self (ADR-0013).

        A Goal/Decision tracks `concept_id` when it references it *or any of its
        ancestors* in the confirmed `broader-of` DAG — so confirming a `broader-of`
        edge that pulls a subtree under "Brain" must notify the Goals tracking
        "Brain" (or anything above it). The scope-widening counterpart of
        `anchors_tracking_relation`. Returns distinct (anchor_type, anchor_id).
        """
        return self._anchors_referencing(set(self.ancestor_concept_ids(concept_id)))

    def _anchors_referencing(
        self, concept_ids: set[int]
    ) -> list[tuple[str, int]]:
        """Distinct Goal/Decision anchors referencing any of `concept_ids`."""
        if not concept_ids:
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT e.src_type, e.src_id FROM edges e "
                "WHERE e.kind = 'references' AND e.dst_type = 'concept' "
                "AND e.src_type IN ('goal', 'decision') AND e.dst_id = ANY(%s) "
                "ORDER BY e.src_type, e.src_id",
                (list(concept_ids),),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]

    def relation_alerts_for_video(
        self,
        video_id: str,
        *,
        now: datetime | None = None,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    ) -> list[NeighbourRelation]:
        """The lateral relationships a freshly-admitted video evidences (ADR-0013).

        Each is returned with its Strength, distinct-creator count, and whether the
        pair is now `contested` (an opposite or `no_effect_on` predicate also holds
        on it, computed over *all* relations on the pair, not just this video's), so
        the alerting pass can derive a stance structurally and gate Tier-2 by
        Strength — no LLM. Strongest first.
        """
        now = now or datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT cr.id, cr.src_concept_id, src.name, cr.predicate, "
                "       cr.dst_concept_id, dst.name "
                "FROM concept_relations cr "
                "JOIN concepts src ON src.id = cr.src_concept_id "
                "JOIN concepts dst ON dst.id = cr.dst_concept_id "
                "JOIN concept_relation_evidence cre ON cre.relation_id = cr.id "
                "JOIN claims cl ON cl.id = cre.claim_id "
                "WHERE cl.video_id = %s",
                (video_id,),
            )
            base = cur.fetchall()
        if not base:
            return []
        return self._relation_metrics(
            [r[0] for r in base], now=now, half_life_days=half_life_days
        )

    def count_relations_touching_subtree(self, concept_id: int) -> int:
        """How many relationships touch a Concept's subtree — the scope-widening
        backlog (ADR-0013). Counts distinct relationships with either endpoint in the
        confirmed `broader-of` subtree of `concept_id` (including itself)."""
        subtree = self.descendant_concept_ids(concept_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM concept_relations "
                "WHERE src_concept_id = ANY(%(ids)s) OR dst_concept_id = ANY(%(ids)s)",
                {"ids": subtree},
            )
            return cur.fetchone()[0]

    def _relation_metrics(
        self, relation_ids: list[int], *, now: datetime, half_life_days: float
    ) -> list[NeighbourRelation]:
        """Build NeighbourRelations (Strength, creator count, contested) for the ids."""
        if not relation_ids:
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cr.id, cr.src_concept_id, src.name, cr.predicate, "
                "       cr.dst_concept_id, dst.name, "
                "       cre.claim_id, v.creator_id, creators.trust_tier, v.published_at "
                "FROM concept_relations cr "
                "JOIN concepts src ON src.id = cr.src_concept_id "
                "JOIN concepts dst ON dst.id = cr.dst_concept_id "
                "JOIN concept_relation_evidence cre ON cre.relation_id = cr.id "
                "JOIN claims cl ON cl.id = cre.claim_id "
                "JOIN videos v ON v.video_id = cl.video_id "
                "JOIN creators ON creators.id = v.creator_id "
                "WHERE cr.id = ANY(%s)",
                (relation_ids,),
            )
            rows = cur.fetchall()
            # All predicates on each touched ordered pair, for the contested check.
            cur.execute(
                "SELECT src_concept_id, dst_concept_id, "
                "       array_agg(DISTINCT predicate) "
                "FROM concept_relations "
                "WHERE (src_concept_id, dst_concept_id) IN ("
                "  SELECT src_concept_id, dst_concept_id FROM concept_relations "
                "  WHERE id = ANY(%s)) "
                "GROUP BY src_concept_id, dst_concept_id",
                (relation_ids,),
            )
            pair_predicates = {(r[0], r[1]): list(r[2]) for r in cur.fetchall()}

        grouped: dict[int, dict] = {}
        for r in rows:
            rel = grouped.setdefault(
                r[0],
                {"src_id": r[1], "src_name": r[2], "predicate": r[3],
                 "dst_id": r[4], "dst_name": r[5], "claim_ids": set(), "contribs": []},
            )
            rel["claim_ids"].add(r[6])
            rel["contribs"].append(
                EvidenceContribution(creator_id=r[7], trust_tier=r[8], dated=r[9])
            )

        result: list[NeighbourRelation] = []
        for relation_id, rel in grouped.items():
            others = pair_predicates.get((rel["src_id"], rel["dst_id"]), [])
            contested = any(contradicts(rel["predicate"], p) for p in others)
            result.append(
                NeighbourRelation(
                    relation_id=relation_id,
                    src_concept_id=rel["src_id"],
                    src_name=rel["src_name"],
                    predicate=rel["predicate"],
                    dst_concept_id=rel["dst_id"],
                    dst_name=rel["dst_name"],
                    strength=relation_strength(
                        rel["contribs"], now=now, half_life_days=half_life_days
                    ),
                    creator_count=distinct_creator_count(rel["contribs"]),
                    contested=contested,
                    evidence_claim_ids=sorted(rel["claim_ids"]),
                )
            )
        result.sort(key=lambda x: (-x.strength, x.relation_id))
        return result

    # -- persistence, inbox & lifecycle (writes/reads) -----------------------

    def add_impact(
        self,
        source_type: str,
        source_id: int,
        anchor_type: str,
        anchor_id: int,
        stance: str,
        *,
        detail: str | None = None,
        tier: int = 1,
    ) -> int | None:
        """Persist a deduped Impact; ``None`` if the same finding already exists.

        The unique constraint `(anchor, source, stance)` makes a re-run or an
        overlapping piece of evidence raise nothing the second time — and a resolved
        Impact's surviving row keeps it from re-nagging (issue #18). `tier` is 1 for
        the push inbox, 2 for the quieter browsable feed (ADR-0013). Does not commit.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO impacts (source_type, source_id, anchor_type, anchor_id, "
                "stance, detail, tier) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (anchor_type, anchor_id, source_type, source_id, stance) "
                "DO NOTHING RETURNING id",
                (source_type, source_id, anchor_type, anchor_id, stance, detail, tier),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def list_impacts(
        self,
        *,
        stance: str | None = None,
        anchor_type: str | None = None,
        anchor_id: int | None = None,
        states: list[str] | None = None,
        tier: int | None = None,
    ) -> list[Impact]:
        """The Impact inbox, newest first — filterable by stance, anchor, state, tier.

        `states` narrows to a lifecycle subset (the default inbox passes the
        unresolved `new`/`reviewed`); `anchor_type`/`anchor_id` filter to one
        anchor; `stance` to one stance; `tier` to the push inbox (1) or the
        browsable feed (2) — ADR-0013. Each Impact carries both ends' labels.
        """
        where: list[str] = []
        params: dict = {}
        if stance is not None:
            where.append("i.stance = %(stance)s")
            params["stance"] = stance
        if anchor_type is not None:
            where.append("i.anchor_type = %(anchor_type)s")
            params["anchor_type"] = anchor_type
        if anchor_id is not None:
            where.append("i.anchor_id = %(anchor_id)s")
            params["anchor_id"] = anchor_id
        if states:
            where.append("i.state = ANY(%(states)s)")
            params["states"] = list(states)
        if tier is not None:
            where.append("i.tier = %(tier)s")
            params["tier"] = tier
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._conn.cursor() as cur:
            cur.execute(
                _IMPACT_SELECT + clause + " ORDER BY i.created_at DESC, i.id DESC",
                params,
            )
            return [_row_to_impact(r) for r in cur.fetchall()]

    def get_impact(self, impact_id: int) -> Impact | None:
        """One Impact with both ends' labels, or ``None`` if it's gone."""
        with self._conn.cursor() as cur:
            cur.execute(_IMPACT_SELECT + " WHERE i.id = %s", (impact_id,))
            row = cur.fetchone()
        return _row_to_impact(row) if row is not None else None

    def set_impact_state(
        self, impact_id: int, state: str, *, actioned_decision_id: int | None = None
    ) -> bool:
        """Move one Impact along its lifecycle (issue #18). ``False`` if it's gone.

        `actioned_decision_id` records the Decision an `actioned` Impact produced;
        passing ``None`` leaves any existing link untouched.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE impacts SET state = %s, updated_at = now(), "
                "actioned_decision_id = COALESCE(%s, actioned_decision_id) "
                "WHERE id = %s",
                (state, actioned_decision_id, impact_id),
            )
            return cur.rowcount > 0

    def bulk_set_impact_state(self, impact_ids: list[int], state: str) -> int:
        """Move many Impacts to `state` at once; returns how many changed (issue #18)."""
        if not impact_ids:
            return 0
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE impacts SET state = %s, updated_at = now() WHERE id = ANY(%s)",
                (state, impact_ids),
            )
            return cur.rowcount

    # -- helpers -------------------------------------------------------------

    def _upsert_creator(self, prov: Provenance) -> int:
        # A video's provenance carries the same stable identity the watch list
        # stores, so archiving and Creator-management share one upsert path.
        # Archiving never adds a Creator to the watch list (issue #69): a watched
        # Creator's row already exists (its flag is left untouched), and a brand-new
        # Creator here can only come from a one-off "Process me" video — so it is
        # created not-subscribed.
        return self.add_creator(
            CreatorIdentity(channel_id=prov.channel_id, name=prov.channel_name),
            subscribed=False,
        )
