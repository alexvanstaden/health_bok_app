"""Creator management + backfill the back-catalogue, end to end (issue #15).

Drives the whole slice against fakes and a real ephemeral Postgres + pgvector:
add/list/remove a Creator, trigger a backfill that surfaces metadata-only
Candidates, bulk-reject obvious noise, and — the heart of the slice — approve a
backfill Candidate that has *no* archived Transcript and watch the worker
transcribe-if-needed (captions, else Whisper) before extracting and admitting,
identical to the daily path. Asserts only on persisted records and observable
lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from health_bok import creators, review
from health_bok.concepts import ConceptNormalizer
from health_bok.models import (
    CandidateMetadata,
    ConceptMention,
    CreatorIdentity,
    ExtractedClaim,
    Extraction,
    FetchedAudio,
    FetchedTranscript,
    Provenance,
    TranscriptSegment,
)
from health_bok.repository import Repository
from health_bok.worker import drain
from tests.fakes import (
    FakeContentSource,
    FakeEmbedder,
    FakeExtractor,
    FakeTranscriber,
)

HUBERMAN = CreatorIdentity(channel_id="UC2D2CMWXMOVWx7giW1n3LIg", name="Huberman Lab")
ATTIA = CreatorIdentity(channel_id="UC8kGsMa0LygSlsDfASTbjBA", name="Peter Attia MD")
HANDLE = "@hubermanlab"
EMBED_MODEL = "fake-embed"

CLAIM_TEXT = "Morning sunlight advances circadian rhythm."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _recent(days: int) -> datetime:
    # Real wall-clock relative, since add_creator/backfill use the wall clock and
    # the default ~2-year cutoff; well inside the window.
    return _now() - timedelta(days=days)


def _provenance(video_id: str) -> Provenance:
    return Provenance(
        video_id=video_id,
        title="Sleep & Light",
        channel_id=HUBERMAN.channel_id,
        channel_name=HUBERMAN.name,
        published_at=_recent(10),
    )


def _captions(video_id: str) -> FetchedTranscript:
    return FetchedTranscript(
        provenance=_provenance(video_id),
        segments=[
            TranscriptSegment(text="Morning light sets your clock.", start=0.0, duration=4.0)
        ],
        source="captions",
    )


def _audio(video_id: str) -> FetchedAudio:
    return FetchedAudio(provenance=_provenance(video_id), data=b"\x00\x01\x02", suffix=".m4a")


def _extraction() -> Extraction:
    return Extraction(
        claims=[
            ExtractedClaim(
                text=CLAIM_TEXT,
                locator_seconds=12,
                type="finding",
                concepts=[ConceptMention(name="circadian rhythm")],
            ),
        ]
    )


def _normalizer(repo: Repository) -> ConceptNormalizer:
    return ConceptNormalizer(FakeEmbedder(), repo, model=EMBED_MODEL)


def _metadata(video_id: str, *, days_ago: int = 10) -> CandidateMetadata:
    return CandidateMetadata(
        video_id=video_id,
        title=f"Episode {video_id}",
        description=f"Notes for {video_id}",
        published_at=_recent(days_ago),
    )


def _seed_backfill_candidate(conn, video_id: str) -> None:
    """Persist HUBERMAN on the watch list with one metadata-only backfill Candidate."""
    repo = Repository(conn)
    creator_id = repo.add_creator(HUBERMAN)
    repo.add_candidate(creator_id, _metadata(video_id))
    repo.commit()


# == Creator management =====================================================


def test_add_list_remove_creator_shows_resolved_channel_name(conn):
    source = FakeContentSource(identities={HANDLE: HUBERMAN, "@peterattia": ATTIA})
    repo = Repository(conn)

    creators.add_creator(HANDLE, content_source=source, repo=repo)
    creators.add_creator("@peterattia", content_source=source, repo=repo)

    # The watch list shows each Creator with its resolved channel name (AC 2).
    watched = Repository(conn).list_creators()
    assert [(c.channel_id, c.name) for c in watched] == [
        (HUBERMAN.channel_id, "Huberman Lab"),
        (ATTIA.channel_id, "Peter Attia MD"),
    ]

    assert creators.remove_creator(HUBERMAN.channel_id, repo=repo) is True
    assert Repository(conn).list_creators() == [ATTIA]


# == Backfill trigger =======================================================


def test_backfill_trigger_surfaces_metadata_only_candidates(conn):
    # Add the Creator with an empty catalogue, then publish a back-catalogue and
    # trigger a backfill explicitly — the Web App's "pull the back-catalogue" path.
    source = FakeContentSource(identities={HANDLE: HUBERMAN})
    repo = Repository(conn)
    creators.add_creator(HANDLE, content_source=source, repo=repo)
    assert Repository(conn).list_backfill_candidates() == []

    source = FakeContentSource(backcatalogue={HUBERMAN.channel_id: [_metadata("vid1")]})
    stored = creators.backfill_creator(
        HUBERMAN.channel_id, content_source=source, repo=Repository(conn)
    )
    assert stored == ["vid1"]

    (candidate,) = Repository(conn).list_backfill_candidates()
    assert candidate.video_id == "vid1"
    assert candidate.title == "Episode vid1"
    assert candidate.description == "Notes for vid1"
    assert candidate.url == "https://www.youtube.com/watch?v=vid1"
    # Metadata-only Candidates carry a thumbnail and the Creator's name (AC 4).
    assert candidate.thumbnail_url == "https://i.ytimg.com/vi/vid1/hqdefault.jpg"
    assert candidate.channel_name == "Huberman Lab"
    assert candidate.state == "candidate"


def test_backfill_trigger_for_unknown_creator_returns_none(conn):
    source = FakeContentSource()
    assert (
        creators.backfill_creator("UCnope", content_source=source, repo=Repository(conn))
        is None
    )


# == Bulk-reject ============================================================


def test_bulk_reject_removes_candidates_and_they_do_not_resurface(conn):
    repo = Repository(conn)
    creator_id = repo.add_creator(HUBERMAN)
    for vid in ("keep", "noise1", "noise2"):
        repo.add_candidate(creator_id, _metadata(vid))
    repo.commit()

    rejected = review.bulk_reject(["noise1", "noise2"], repo=repo)
    assert rejected == 2

    # The rejected Candidates leave the backfill queue; the kept one stays (AC 5).
    assert [c.video_id for c in Repository(conn).list_backfill_candidates()] == ["keep"]

    # Re-running backfill over the same catalogue does not resurface rejected noise.
    source = FakeContentSource(
        backcatalogue={HUBERMAN.channel_id: [_metadata(v) for v in ("keep", "noise1", "noise2")]}
    )
    creators.backfill_creator(HUBERMAN.channel_id, content_source=source, repo=Repository(conn))
    assert [c.video_id for c in Repository(conn).list_backfill_candidates()] == ["keep"]


# == Bulk-approve ===========================================================


def test_bulk_approve_enqueues_each_and_skips_in_flight(conn):
    repo = Repository(conn)
    creator_id = repo.add_creator(HUBERMAN)
    for vid in ("a", "b", "c"):
        repo.add_candidate(creator_id, _metadata(vid))
    repo.commit()

    # "a" is already approved (e.g. via per-video Approve); bulk-approving the
    # selection enqueues "b" and "c" but skips the in-flight "a" (issue #73).
    review.approve_candidate("a", repo=repo)
    approved = review.bulk_approve(["a", "b", "c"], repo=repo)
    assert approved == 2

    # All three are now approved, with exactly one admission job each — no duplicate
    # for "a" (ADR-0004, ADR-0010).
    repo = Repository(conn)
    assert {c.video_id: c.state for c in repo.list_backfill_candidates()} == {
        "a": "approved",
        "b": "approved",
        "c": "approved",
    }
    assert _job_count(conn) == 3


def _job_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        return cur.fetchone()[0]


# == Approve → transcribe-if-needed → extract → admit =======================


def test_approving_a_backfill_candidate_transcribes_from_captions_then_admits(conn):
    video_id = "bf_captions"
    _seed_backfill_candidate(conn, video_id)
    repo = Repository(conn)

    # Approve flows into the *same* pipeline as a daily Candidate (AC 6).
    assert review.approve_candidate(video_id, repo=repo) is True
    assert repo.admission_state(video_id) == "approved"

    # The worker acquires the Transcript (captions present → Whisper untouched),
    # archives it, then extracts and admits.
    source = FakeContentSource(transcripts={video_id: _captions(video_id)})
    transcriber = FakeTranscriber()
    handled = drain(
        content_source=source,
        transcriber=transcriber,
        extractor=FakeExtractor(_extraction()),
        normalizer=_normalizer(repo),
        repo=repo,
    )

    assert handled == 1
    assert source.fetched_video_ids == [video_id]  # captions fetched once
    assert transcriber.transcribed == []  # captions present → no Whisper
    assert repo.admission_state(video_id) == "admitted"

    # The Transcript is now archived (captions), and the Claim is admitted with a
    # locator deep-link — identical end state to a daily Candidate.
    assert _transcript_source(conn, video_id) == "captions"
    claims = repo.admitted_claims(video_id)
    assert [c.text for c in claims] == [CLAIM_TEXT]
    assert claims[0].deep_link == f"https://www.youtube.com/watch?v={video_id}&t=12s"
    # An admitted backfill Candidate leaves the backfill review queue.
    assert video_id not in {c.video_id for c in repo.list_backfill_candidates()}


def test_approving_a_caption_less_backfill_candidate_uses_whisper(conn):
    video_id = "bf_whisper"
    _seed_backfill_candidate(conn, video_id)
    repo = Repository(conn)
    review.approve_candidate(video_id, repo=repo)

    # No captions for this video → the worker downloads audio and Whisper runs:
    # backfill is where transcribe-if-needed genuinely fires (AC 6, user story 10).
    source = FakeContentSource(audio={video_id: _audio(video_id)})
    transcriber = FakeTranscriber(
        [TranscriptSegment(text="whispered words", start=0.0, duration=2.0)]
    )
    drain(
        content_source=source,
        transcriber=transcriber,
        extractor=FakeExtractor(_extraction()),
        normalizer=_normalizer(repo),
        repo=repo,
    )

    assert source.fetched_video_ids == [video_id]  # captions checked first...
    assert source.audio_fetched == [video_id]  # ...absent, so audio downloaded...
    assert transcriber.transcribed == [video_id]  # ...and Whisper transcribed it.
    assert _transcript_source(conn, video_id) == "whisper"
    assert repo.admission_state(video_id) == "admitted"
    assert [c.text for c in repo.admitted_claims(video_id)] == [CLAIM_TEXT]


def test_failed_backfill_admission_keeps_transcript_so_retry_does_not_re_transcribe(conn):
    video_id = "bf_retry"
    _seed_backfill_candidate(conn, video_id)
    repo = Repository(conn)
    review.approve_candidate(video_id, repo=repo)

    source = FakeContentSource(transcripts={video_id: _captions(video_id)})
    transcriber = FakeTranscriber()

    # First drain: a raising Extractor fails admission *after* the Transcript was
    # acquired and durably archived.
    drain(
        content_source=source,
        transcriber=transcriber,
        extractor=FakeExtractor(error=RuntimeError("model unavailable")),
        normalizer=_normalizer(repo),
        repo=repo,
    )
    assert repo.admission_state(video_id) == "failed"
    assert _transcript_source(conn, video_id) == "captions"  # archive survived
    assert repo.admitted_claims(video_id) == []
    # A failed backfill Candidate stays visible for retry.
    assert {c.video_id: c.state for c in repo.list_backfill_candidates()}[video_id] == "failed"

    # Retry: a working Extractor admits it, and the Transcript is *not* re-acquired.
    assert review.retry_candidate(video_id, repo=repo) is True
    drain(
        content_source=source,
        transcriber=transcriber,
        extractor=FakeExtractor(_extraction()),
        normalizer=_normalizer(repo),
        repo=repo,
    )
    assert repo.admission_state(video_id) == "admitted"
    assert [c.text for c in repo.admitted_claims(video_id)] == [CLAIM_TEXT]
    assert source.fetched_video_ids == [video_id]  # acquired exactly once, not on retry


def _transcript_source(conn, video_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT transcript_source FROM videos WHERE video_id = %s", (video_id,)
        )
        row = cur.fetchone()
    return row[0] if row else ""
