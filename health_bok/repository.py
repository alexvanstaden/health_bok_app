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

from .models import CreatorIdentity, FetchedTranscript, Provenance, TranscriptSegment


@dataclass(frozen=True)
class ArchivedSummary:
    """A persisted Summary, read back for assembling the Digest."""

    video_id: str
    title: str
    url: str
    body: str


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

    # -- helpers -------------------------------------------------------------

    def _upsert_creator(self, prov: Provenance) -> int:
        # A video's provenance carries the same stable identity the watch list
        # stores, so archiving and Creator-management share one upsert path.
        return self.add_creator(
            CreatorIdentity(channel_id=prov.channel_id, name=prov.channel_name)
        )
