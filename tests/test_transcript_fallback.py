"""The captions-vs-Whisper fallback decision (issue #5).

A focused lower-seam test (PRD #1 testing decisions): drive the daily job with a
faked `ContentSource` and `Transcriber` plus a real ephemeral Postgres, toggling
only whether the video has captions, and assert which acquisition path is taken
and that the chosen source is recorded on the video's provenance (user story 32).
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok.job import run_job
from health_bok.models import (
    CreatorIdentity,
    FetchedAudio,
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
CREATOR = CreatorIdentity(channel_id="UC_fallback", name="Fallback Lab")
PUBLISHED_AT = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def _provenance(video_id: str) -> Provenance:
    return Provenance(
        video_id=video_id,
        title=f"Video {video_id}",
        channel_id=CREATOR.channel_id,
        channel_name=CREATOR.name,
        published_at=PUBLISHED_AT,
    )


def _captioned(video_id: str) -> FetchedTranscript:
    return FetchedTranscript(
        provenance=_provenance(video_id),
        segments=[TranscriptSegment(text="from captions", start=0.0, duration=1.0)],
        source="captions",
    )


def _audio(video_id: str) -> FetchedAudio:
    return FetchedAudio(provenance=_provenance(video_id), data=b"audio-bytes", suffix=".m4a")


def _seed(repo: Repository) -> None:
    repo.add_creator(CREATOR)
    repo.commit()


def _run(repo, source, transcriber):
    return run_job(
        content_source=source,
        transcriber=transcriber,
        summarizer=FakeSummarizer("summary"),
        digest_sender=FakeDigestSender(),
        repo=repo,
        model=MODEL,
    )


def _transcript_source(conn, video_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT transcript_source FROM videos WHERE video_id = %s", (video_id,))
        return cur.fetchone()[0]


def test_captions_present_are_used_and_whisper_is_not_called(conn):
    """Captions available -> they're used, Whisper untouched (AC 1, 3)."""
    repo = Repository(conn)
    _seed(repo)
    source = FakeContentSource(
        feeds={CREATOR.channel_id: ["has_caps"]},
        transcripts={"has_caps": _captioned("has_caps")},
    )
    transcriber = FakeTranscriber()

    result = _run(repo, source, transcriber)

    assert result.newly_processed == ["has_caps"]
    # The Whisper path was never touched: no audio download, no transcription.
    assert transcriber.transcribed == []
    assert source.audio_fetched == []
    # Provenance records captions as the source (user story 32).
    assert _transcript_source(conn, "has_caps") == "captions"
    segments = repo.load_transcript_segments("has_caps")
    assert [s.text for s in segments] == ["from captions"]


def test_no_captions_falls_back_to_whisper_and_records_the_source(conn):
    """No captions -> audio is downloaded and Whisper transcribes it (AC 2, 3)."""
    repo = Repository(conn)
    _seed(repo)
    # "no_caps" is known only as audio, so fetch_transcript returns None for it.
    source = FakeContentSource(
        feeds={CREATOR.channel_id: ["no_caps"]},
        audio={"no_caps": _audio("no_caps")},
    )
    transcriber = FakeTranscriber(
        segments=[TranscriptSegment(text="from whisper", start=0.0, duration=2.0)]
    )

    result = _run(repo, source, transcriber)

    assert result.newly_processed == ["no_caps"]
    # The fallback ran: captions were checked, audio downloaded, Whisper called.
    assert source.fetched_video_ids == ["no_caps"]
    assert source.audio_fetched == ["no_caps"]
    assert transcriber.transcribed == ["no_caps"]
    # Provenance records Whisper as the source, and the Whisper text is archived.
    assert _transcript_source(conn, "no_caps") == "whisper"
    segments = repo.load_transcript_segments("no_caps")
    assert [s.text for s in segments] == ["from whisper"]


def test_each_video_takes_its_own_path_in_one_run(conn):
    """Within a run, captioned videos skip Whisper while caption-less ones use it."""
    repo = Repository(conn)
    _seed(repo)
    source = FakeContentSource(
        feeds={CREATOR.channel_id: ["caps", "no_caps"]},
        transcripts={"caps": _captioned("caps")},
        audio={"no_caps": _audio("no_caps")},
    )
    transcriber = FakeTranscriber()

    result = _run(repo, source, transcriber)

    assert set(result.newly_processed) == {"caps", "no_caps"}
    # Whisper ran for exactly the caption-less video.
    assert transcriber.transcribed == ["no_caps"]
    assert source.audio_fetched == ["no_caps"]
    assert _transcript_source(conn, "caps") == "captions"
    assert _transcript_source(conn, "no_caps") == "whisper"
