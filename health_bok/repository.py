"""Persistence against the single source-of-truth Postgres (ADR-0003).

The store is deliberately *not* a port: integration tests run it for real
(PRD #1). All writes for one video commit together so a crash never leaves a
half-archived video, keeping the job idempotent and crash-safe (user story 22).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import psycopg

from .models import (
    CandidateMetadata,
    CreatorIdentity,
    FetchedTranscript,
    Provenance,
    TranscriptSegment,
    locator_url,
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


def _vector_literal(embedding: list[float]) -> str:
    """Render a Python vector as a pgvector text literal (cast `::vector` in SQL).

    psycopg has no native pgvector type, so embeddings cross the boundary as the
    `[0.1,0.2,…]` literal pgvector parses; keeping this in one place stops the
    format leaking into the queries.
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


@dataclass(frozen=True)
class StoredCandidate:
    """A persisted backfill Candidate, read back for the approval queue / tests.

    Metadata only — no Transcript or Summary — carrying the Creator's stable
    `channel_id` so a Candidate stays attributable to whom it was backfilled for.
    """

    video_id: str
    channel_id: str
    title: str
    description: str
    url: str
    published_at: datetime


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
        """Return every watched Creator's stable identity, oldest first.

        This is the watch list the daily job reads to know whom to poll
        (PRD #1, user story 5).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT channel_id, name FROM creators ORDER BY created_at, id"
            )
            return [CreatorIdentity(channel_id=r[0], name=r[1]) for r in cur.fetchall()]

    def list_candidates(self) -> list[StoredCandidate]:
        """Every stored backfill Candidate, newest published first.

        The owner's approval queue reads this (a later slice); the backfill tests
        assert on it to confirm only metadata is stored and the cutoff is honored.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT c.video_id, cr.channel_id, c.title, c.description, "
                "       c.url, c.published_at "
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
                )
                for r in cur.fetchall()
            ]

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

    def add_creator(self, identity: CreatorIdentity) -> int:
        """Persist a Creator by stable identity; idempotent on channel_id.

        Re-adding an existing Creator refreshes its display name but never
        creates a duplicate (PRD #1, user stories 3-4). Like the other writes,
        this does not commit — the caller owns the transaction boundary.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO creators (channel_id, name) VALUES (%s, %s) "
                "ON CONFLICT (channel_id) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id",
                (identity.channel_id, identity.name),
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

    def admission_state(self, video_id: str) -> str:
        """The Candidate's lifecycle state — `candidate` when no row exists yet."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM admissions WHERE video_id = %s", (video_id,)
            )
            row = cur.fetchone()
        return row[0] if row else CANDIDATE

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

    # -- helpers -------------------------------------------------------------

    def _upsert_creator(self, prov: Provenance) -> int:
        # A video's provenance carries the same stable identity the watch list
        # stores, so archiving and Creator-management share one upsert path.
        return self.add_creator(
            CreatorIdentity(channel_id=prov.channel_id, name=prov.channel_name)
        )
