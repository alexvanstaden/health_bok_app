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

from .models import FetchedTranscript, Provenance, TranscriptSegment


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

    # -- reads ---------------------------------------------------------------

    def is_processed(self, video_id: str) -> bool:
        """True once both Transcript and Summary are durably persisted.

        Used for idempotency: a processed video is never re-fetched or
        re-summarized on a repeat run (user story 23).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT summarized_at IS NOT NULL "
                "FROM processing_state WHERE video_id = %s",
                (video_id,),
            )
            row = cur.fetchone()
        return bool(row and row[0])

    def digest_already_sent(self, video_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT digest_sent_at IS NOT NULL "
                "FROM processing_state WHERE video_id = %s",
                (video_id,),
            )
            row = cur.fetchone()
        return bool(row and row[0])

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
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO creators (channel_id, name) VALUES (%s, %s) "
                "ON CONFLICT (channel_id) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id",
                (prov.channel_id, prov.channel_name),
            )
            return cur.fetchone()[0]
