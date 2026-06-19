"""Integration tests for one-off "Process me" playlist ingestion (issue #69).

Drives the daily job with a configured playlist against faked ports and a real
ephemeral Postgres, asserting only on what gets persisted (PRD #1 testing
decisions). Covers the seven acceptance criteria: a playlist video flows through
the same transcribe → summarize → Candidate path as a watched Creator; its
Creator is recorded but stays off the watch list and is never polled or
backfilled; one-off Creators are full Creators for attribution (default trust-tier
1); re-runs create no duplicates; an already-subscribed Creator's playlist video
is processed without flipping its flag; the watched-Creator flow is unaffected;
and with no playlist configured behavior is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from health_bok import creators
from health_bok.job import run_job
from health_bok.models import (
    CreatorIdentity,
    FetchedTranscript,
    Provenance,
    TranscriptSegment,
)
from health_bok.repository import Repository
from tests.fakes import (
    FakeContentSource,
    FakeDigestSender,
    FakeSummarizer,
    FakeTranscriber,
)

MODEL = "claude-sonnet-4-6"
PLAYLIST = "PL_process_me"

HUBERMAN = CreatorIdentity(channel_id="UC_huberman", name="Huberman Lab")
# The Creator behind a one-off playlist video — never on the watch list.
RHONDA = CreatorIdentity(channel_id="UC_rhonda", name="FoundMyFitness")

BASE_PUBLISHED = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def _transcript(video_id: str, creator: CreatorIdentity, *, day: int) -> FetchedTranscript:
    provenance = Provenance(
        video_id=video_id,
        title=f"Video {video_id}",
        channel_id=creator.channel_id,
        channel_name=creator.name,
        published_at=BASE_PUBLISHED + timedelta(days=day),
    )
    return FetchedTranscript(
        provenance=provenance,
        segments=[TranscriptSegment(text=f"content of {video_id}", start=0.0, duration=1.0)],
        source="captions",
    )


def _run(repo, content_source, *, summarizer=None, digest_sender=None, playlist=PLAYLIST):
    return run_job(
        content_source=content_source,
        transcriber=FakeTranscriber(),
        summarizer=summarizer or FakeSummarizer("a summary"),
        digest_sender=digest_sender or FakeDigestSender(),
        repo=repo,
        model=MODEL,
        process_me_playlist_id=playlist,
    )


def _processed_video_ids(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT video_id FROM processing_state WHERE summarized_at IS NOT NULL")
        return {r[0] for r in cur.fetchall()}


def _subscribed(conn, channel_id: str) -> bool | None:
    with conn.cursor() as cur:
        cur.execute("SELECT subscribed FROM creators WHERE channel_id = %s", (channel_id,))
        row = cur.fetchone()
    return row[0] if row else None


def test_playlist_video_is_processed_through_the_same_path(conn):
    """A new playlist video is transcribed + summarized like a watched upload (AC 2)."""
    repo = Repository(conn)
    source = FakeContentSource(
        playlists={PLAYLIST: ["oneoff"]},
        transcripts={"oneoff": _transcript("oneoff", RHONDA, day=1)},
    )

    result = _run(repo, source)

    assert result.newly_processed == ["oneoff"]
    assert source.discovered_playlists == [PLAYLIST]
    # It reaches the review queue exactly as a daily Candidate does: a Summary.
    assert _processed_video_ids(conn) == {"oneoff"}
    assert Repository(conn).get_summary("oneoff").body == "a summary"


def test_one_off_creator_is_off_the_watch_list_and_not_polled_or_backfilled(conn):
    """The one-off Creator is stored but never watched (AC 3)."""
    repo = Repository(conn)
    source = FakeContentSource(
        playlists={PLAYLIST: ["oneoff"]},
        transcripts={"oneoff": _transcript("oneoff", RHONDA, day=1)},
    )

    _run(repo, source)

    fresh = Repository(conn)
    # Recorded for attribution, but not-subscribed and absent from the watch list.
    assert _subscribed(conn, RHONDA.channel_id) is False
    assert fresh.list_creators() == []
    # The daily poll never discovered its channel — only the playlist was read.
    assert source.discovered == []
    # And a backfill trigger treats it as absent (404), never listing its catalogue.
    assert (
        creators.backfill_creator(RHONDA.channel_id, content_source=source, repo=fresh)
        is None
    )
    assert source.listed == []


def test_one_off_creator_is_attributable_at_default_trust_tier(conn):
    """The one-off Creator is a full Creator for Strength: default trust-tier 1 (AC 4)."""
    repo = Repository(conn)
    source = FakeContentSource(
        playlists={PLAYLIST: ["oneoff"]},
        transcripts={"oneoff": _transcript("oneoff", RHONDA, day=1)},
    )

    _run(repo, source)

    fresh = Repository(conn)
    # creator_id resolves it (unfiltered), so its Claims can be cited and counted.
    creator_id = fresh.creator_id(RHONDA.channel_id)
    assert creator_id is not None
    with conn.cursor() as cur:
        cur.execute("SELECT trust_tier FROM creators WHERE id = %s", (creator_id,))
        assert cur.fetchone()[0] == 1
    # The video Source is attributed to that Creator.
    with conn.cursor() as cur:
        cur.execute("SELECT creator_id FROM videos WHERE video_id = 'oneoff'")
        assert cur.fetchone()[0] == creator_id


def test_rerun_creates_no_duplicates(conn):
    """Re-running with the same playlist contents reprocesses nothing (AC 5)."""
    repo = Repository(conn)
    source = FakeContentSource(
        playlists={PLAYLIST: ["oneoff"]},
        transcripts={"oneoff": _transcript("oneoff", RHONDA, day=1)},
    )
    _run(repo, source)

    second = FakeContentSource(
        playlists={PLAYLIST: ["oneoff"]},
        transcripts={"oneoff": _transcript("oneoff", RHONDA, day=1)},
    )
    result = _run(repo, second)

    assert result.newly_processed == []
    assert second.fetched_video_ids == []  # the known video is not re-fetched
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM videos WHERE video_id = 'oneoff'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM summaries WHERE video_id = 'oneoff'")
        assert cur.fetchone()[0] == 1


def test_playlist_video_of_already_subscribed_creator_keeps_it_subscribed(conn):
    """A playlist video by a watched Creator is processed; the flag is unchanged (AC 6)."""
    repo = Repository(conn)
    repo.add_creator(HUBERMAN)  # already subscribed
    repo.commit()

    source = FakeContentSource(
        feeds={HUBERMAN.channel_id: []},  # nothing new on the daily feed today
        playlists={PLAYLIST: ["hub_oneoff"]},
        transcripts={"hub_oneoff": _transcript("hub_oneoff", HUBERMAN, day=2)},
    )
    result = _run(repo, source)

    assert result.newly_processed == ["hub_oneoff"]
    # The Creator stays subscribed and on the watch list — no flip to one-off.
    assert _subscribed(conn, HUBERMAN.channel_id) is True
    assert Repository(conn).list_creators() == [HUBERMAN]


def test_watched_creator_flow_is_unaffected_by_playlist_ingestion(conn):
    """Watched uploads and playlist videos are both processed in one tick (AC 7)."""
    repo = Repository(conn)
    repo.add_creator(HUBERMAN)
    repo.commit()

    source = FakeContentSource(
        feeds={HUBERMAN.channel_id: ["new_h"]},
        playlists={PLAYLIST: ["oneoff"]},
        transcripts={
            "new_h": _transcript("new_h", HUBERMAN, day=2),
            "oneoff": _transcript("oneoff", RHONDA, day=1),
        },
    )
    result = _run(repo, source)

    assert set(result.newly_processed) == {"new_h", "oneoff"}
    assert source.discovered == [HUBERMAN.channel_id]  # watched poll still runs
    assert _processed_video_ids(conn) == {"new_h", "oneoff"}


def test_already_known_candidate_is_skipped(conn):
    """A playlist video already known as a backfill Candidate is not reprocessed (AC 5)."""
    repo = Repository(conn)
    # Seed RHONDA's "oneoff" as a metadata-only backfill Candidate of a watched Creator.
    creator_id = repo.add_creator(RHONDA)
    from health_bok.models import CandidateMetadata

    repo.add_candidate(
        creator_id,
        CandidateMetadata(
            video_id="oneoff",
            title="Already a candidate",
            description="",
            published_at=BASE_PUBLISHED,
        ),
    )
    repo.commit()

    source = FakeContentSource(
        playlists={PLAYLIST: ["oneoff"]},
        transcripts={"oneoff": _transcript("oneoff", RHONDA, day=1)},
    )
    result = _run(repo, source)

    assert result.newly_processed == []
    assert source.fetched_video_ids == []  # the known candidate is never fetched


def test_no_playlist_configured_leaves_the_run_unchanged(conn):
    """With no playlist set, the playlist feed is never read (AC 1)."""
    repo = Repository(conn)
    repo.add_creator(HUBERMAN)
    repo.commit()

    source = FakeContentSource(
        feeds={HUBERMAN.channel_id: ["new_h"]},
        playlists={PLAYLIST: ["oneoff"]},
        transcripts={
            "new_h": _transcript("new_h", HUBERMAN, day=2),
            "oneoff": _transcript("oneoff", RHONDA, day=1),
        },
    )
    result = _run(repo, source, playlist="")

    assert result.newly_processed == ["new_h"]
    assert source.discovered_playlists == []  # the playlist was never touched
    assert _processed_video_ids(conn) == {"new_h"}


def test_playlist_discovery_failure_is_isolated(conn):
    """A playlist feed that errors is recorded as a failure, not fatal (AC 7)."""
    repo = Repository(conn)
    repo.add_creator(HUBERMAN)
    repo.commit()

    source = FakeContentSource(
        feeds={HUBERMAN.channel_id: ["new_h"]},
        transcripts={"new_h": _transcript("new_h", HUBERMAN, day=2)},
        errors={PLAYLIST: RuntimeError("playlist feed unreachable")},
    )
    result = _run(repo, source)

    # The watched upload still processes; the playlist failure is isolated.
    assert result.newly_processed == ["new_h"]
    assert [f.scope for f in result.failures] == [PLAYLIST]
