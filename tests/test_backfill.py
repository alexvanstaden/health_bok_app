"""Backfill Candidate population tests (issue #7 / slice 6).

Drives the backfill service and the Creator-add path it hangs off against a faked
ContentSource back-catalogue plus a real ephemeral Postgres, asserting on what
gets persisted — metadata-only Candidates, the recency cutoff honored, and *no*
Transcript/Summary or transcription anywhere (PRD #1, user story 29; ADR-0004).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from health_bok import creators
from health_bok.backfill import backfill_candidates
from health_bok.models import CandidateMetadata, CreatorIdentity
from health_bok.repository import Repository
from tests.fakes import FakeContentSource

HUBERMAN = CreatorIdentity(channel_id="UC2D2CMWXMOVWx7giW1n3LIg", name="Huberman Lab")
ATTIA = CreatorIdentity(channel_id="UC8kGsMa0LygSlsDfASTbjBA", name="Peter Attia MD")
HANDLE = "@hubermanlab"

# A fixed clock so the cutoff arithmetic is deterministic.
NOW = datetime(2026, 6, 9, tzinfo=timezone.utc)
TWO_YEARS = timedelta(days=730)


def _at(days_ago: int) -> datetime:
    return NOW - timedelta(days=days_ago)


def _candidate(video_id: str, *, days_ago: int, **kw) -> CandidateMetadata:
    return CandidateMetadata(
        video_id=video_id,
        title=kw.get("title", f"Episode {video_id}"),
        description=kw.get("description", f"Notes for {video_id}"),
        published_at=_at(days_ago),
    )


def _counts(conn) -> dict[str, int]:
    """Row counts for every table backfill must leave empty — no raw content."""
    with conn.cursor() as cur:
        counts = {}
        for table in ("videos", "transcripts", "summaries", "processing_state"):
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
    return counts


def _seed_creator(conn, identity: CreatorIdentity) -> int:
    repo = Repository(conn)
    creator_id = repo.add_creator(identity)
    repo.commit()
    return creator_id


def test_backfill_stores_metadata_only_candidates_within_cutoff(conn):
    creator_id = _seed_creator(conn, HUBERMAN)
    source = FakeContentSource(
        backcatalogue={
            HUBERMAN.channel_id: [
                _candidate("recent", days_ago=30),
                _candidate("edge", days_ago=700),  # still inside the 2y window
                _candidate("old", days_ago=800),  # just outside — dropped
                _candidate("ancient", days_ago=1500),  # well outside — dropped
            ]
        },
    )
    repo = Repository(conn)

    stored = backfill_candidates(
        creator_id,
        HUBERMAN.channel_id,
        content_source=source,
        repo=repo,
        cutoff=TWO_YEARS,
        now=lambda: NOW,
    )
    repo.commit()

    # Only the in-window uploads are kept — the cutoff is honored (AC 1, AC 4).
    assert stored == ["recent", "edge"]
    candidates = Repository(conn).list_candidates()
    assert [c.video_id for c in candidates] == ["recent", "edge"]  # newest first
    # Metadata only — and not a single Transcript/Summary/video row (AC 2, AC 3).
    assert _counts(conn) == {
        "videos": 0,
        "transcripts": 0,
        "summaries": 0,
        "processing_state": 0,
    }


def test_candidates_carry_full_metadata_and_attribution(conn):
    creator_id = _seed_creator(conn, HUBERMAN)
    source = FakeContentSource(
        backcatalogue={
            HUBERMAN.channel_id: [
                CandidateMetadata(
                    video_id="abc123",
                    title="Sleep & Light",
                    description="Morning sunlight and circadian rhythm.",
                    published_at=_at(10),
                )
            ]
        },
    )
    repo = Repository(conn)
    backfill_candidates(
        creator_id, HUBERMAN.channel_id, content_source=source, repo=repo, now=lambda: NOW
    )
    repo.commit()

    (candidate,) = Repository(conn).list_candidates()
    assert candidate.video_id == "abc123"
    assert candidate.channel_id == HUBERMAN.channel_id  # attributed to the Creator
    assert candidate.title == "Sleep & Light"
    assert candidate.description == "Morning sunlight and circadian rhythm."
    assert candidate.url == "https://www.youtube.com/watch?v=abc123"
    assert candidate.published_at == _at(10)


def test_backfill_never_transcribes(conn):
    creator_id = _seed_creator(conn, HUBERMAN)
    source = FakeContentSource(
        backcatalogue={HUBERMAN.channel_id: [_candidate("v1", days_ago=5)]},
    )
    repo = Repository(conn)

    backfill_candidates(
        creator_id, HUBERMAN.channel_id, content_source=source, repo=repo, now=lambda: NOW
    )

    # Backfill lists the catalogue but touches neither captions nor audio (AC 3).
    assert source.listed == [HUBERMAN.channel_id]
    assert source.fetched_video_ids == []
    assert source.audio_fetched == []


def test_backfill_is_idempotent_on_video_id(conn):
    creator_id = _seed_creator(conn, HUBERMAN)
    source = FakeContentSource(
        backcatalogue={HUBERMAN.channel_id: [_candidate("v1", days_ago=5)]},
    )
    repo = Repository(conn)

    backfill_candidates(
        creator_id, HUBERMAN.channel_id, content_source=source, repo=repo, now=lambda: NOW
    )
    repo.commit()
    second = backfill_candidates(
        creator_id, HUBERMAN.channel_id, content_source=source, repo=repo, now=lambda: NOW
    )
    repo.commit()

    # A re-run re-asserts nothing new — one row, not two.
    assert second == []
    assert [c.video_id for c in Repository(conn).list_candidates()] == ["v1"]


def test_adding_a_creator_populates_its_backcatalogue(conn):
    # Dates relative to real now, since add_creator uses the wall clock and the
    # default ~2-year cutoff; the boundary is far from both test dates.
    now = datetime.now(timezone.utc)
    source = FakeContentSource(
        identities={HANDLE: HUBERMAN},
        backcatalogue={
            HUBERMAN.channel_id: [
                CandidateMetadata(
                    video_id="fresh",
                    title="Recent",
                    description="kept",
                    published_at=now - timedelta(days=20),
                ),
                CandidateMetadata(
                    video_id="stale",
                    title="Old",
                    description="dropped",
                    published_at=now - timedelta(days=900),
                ),
            ]
        },
    )
    repo = Repository(conn)

    identity = creators.add_creator(HANDLE, content_source=source, repo=repo)

    assert identity == HUBERMAN
    assert source.listed == [HUBERMAN.channel_id]
    assert [c.video_id for c in Repository(conn).list_candidates()] == ["fresh"]
    # Adding a Creator backfills metadata only — it never transcribes (AC 3).
    assert source.fetched_video_ids == []
    assert source.audio_fetched == []


def test_adding_a_creator_with_no_backcatalogue_still_adds_the_creator(conn):
    source = FakeContentSource(identities={HANDLE: HUBERMAN})
    repo = Repository(conn)

    creators.add_creator(HANDLE, content_source=source, repo=repo)

    assert Repository(conn).list_creators() == [HUBERMAN]
    assert Repository(conn).list_candidates() == []


def test_candidates_are_attributed_per_creator(conn):
    huberman_id = _seed_creator(conn, HUBERMAN)
    attia_id = _seed_creator(conn, ATTIA)
    source = FakeContentSource(
        backcatalogue={
            HUBERMAN.channel_id: [_candidate("h1", days_ago=5)],
            ATTIA.channel_id: [_candidate("a1", days_ago=5)],
        },
    )
    repo = Repository(conn)
    backfill_candidates(
        huberman_id, HUBERMAN.channel_id, content_source=source, repo=repo, now=lambda: NOW
    )
    backfill_candidates(
        attia_id, ATTIA.channel_id, content_source=source, repo=repo, now=lambda: NOW
    )
    repo.commit()

    by_video = {c.video_id: c.channel_id for c in Repository(conn).list_candidates()}
    assert by_video == {"h1": HUBERMAN.channel_id, "a1": ATTIA.channel_id}
