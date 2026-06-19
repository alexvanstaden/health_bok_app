"""The Logs page's read model: admitted/failed video Sources, newest-first (issue #33).

The Logs page is a read-only record of every video the pipeline carried to a terminal
admission, whether or not it carries a Summary (issue #79). Its whole behaviour lives
in one repository query — videos ⋈ creators ⋈ admission state, latest Summary
left-joined so its absence drops the body to None — which the `GET /api/videos`
endpoint serialises
verbatim (the thin-serialisation pattern the API uses throughout, and which the test
suite deliberately does not import — see `health_bok/api.py`). So these drive the query
directly against a real Postgres: ordering, the BoK-state badge (`admitted`/`failed`),
and that videos still in flight or never approved are hidden.
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok.repository import Repository
from tests.seed import seed_processed_video


def _at(day: int) -> datetime:
    return datetime(2026, 3, day, 12, 0, tzinfo=timezone.utc)


def test_empty_system_returns_no_rows(conn):
    assert Repository(conn).list_processed_videos() == []


def test_admitted_and_failed_listed_newest_added_first(conn):
    repo = Repository(conn)

    # An admitted video: it reached the Body of Knowledge.
    seed_processed_video(
        repo,
        video_id="vid_admitted",
        channel_id="UC_lab",
        channel_name="Longevity Lab",
        title="Zone 2 Cardio Explained",
        summary="Zone 2 training raises mitochondrial density.",
        retrieved_at=_at(1),
    )
    repo.set_admission("vid_admitted", "admitted")
    repo.commit()

    # A failed video: processed, approved, but extraction errored — still a record.
    seed_processed_video(
        repo,
        video_id="vid_failed",
        channel_id="UC_sleep",
        channel_name="Sleep Science",
        summary="Magnesium glycinate before bed.",
        retrieved_at=_at(2),
    )
    repo.set_admission("vid_failed", "failed", error="extractor blew up")
    repo.commit()

    videos = repo.list_processed_videos()

    # Newest-added first (by retrieved_at).
    assert [v.video_id for v in videos] == ["vid_failed", "vid_admitted"]

    by_id = {v.video_id: v for v in videos}
    assert by_id["vid_admitted"].bok_state == "admitted"
    assert by_id["vid_failed"].bok_state == "failed"

    # Each row carries the title, Creator name, the date added, and latest Summary.
    assert by_id["vid_admitted"].title == "Zone 2 Cardio Explained"
    assert by_id["vid_admitted"].creator_name == "Longevity Lab"
    assert by_id["vid_admitted"].added_at == _at(1)
    assert "mitochondrial density" in by_id["vid_admitted"].summary


def test_admitted_without_summary_is_listed(conn):
    repo = Repository(conn)

    # A backfill admission reaches the Body of Knowledge without ever being
    # summarized: it has a Transcript and a terminal admission but no Summary row.
    # It must still appear on the log, with summary left None (issue #79).
    seed_processed_video(
        repo,
        video_id="vid_no_summary",
        channel_name="Backfill Creator",
        title="An Admitted Backfill Video",
        summary=None,
        retrieved_at=_at(3),
    )
    repo.set_admission("vid_no_summary", "admitted")
    repo.commit()

    [video] = repo.list_processed_videos()
    assert video.video_id == "vid_no_summary"
    assert video.bok_state == "admitted"
    assert video.summary is None
    assert video.title == "An Admitted Backfill Video"
    assert video.creator_name == "Backfill Creator"


def test_in_flight_and_never_approved_videos_are_hidden(conn):
    repo = Repository(conn)
    # A processed video with no admission row (a daily Candidate awaiting approval)
    # and ones still in flight / declined must not show in the log.
    seed_processed_video(repo, video_id="vid_plain", retrieved_at=_at(1))
    for vid, state in [
        ("vid_approved", "approved"),
        ("vid_processing", "processing"),
        ("vid_rejected", "rejected"),
    ]:
        seed_processed_video(
            repo, video_id=vid, channel_id=f"UC_{vid}", retrieved_at=_at(1)
        )
        repo.set_admission(vid, state)
        repo.commit()

    assert repo.list_processed_videos() == []


def test_latest_summary_wins(conn):
    repo = Repository(conn)
    seed_processed_video(
        repo, video_id="vid_resummarized", summary="The first, older summary."
    )
    repo.save_summary(
        "vid_resummarized",
        "The newer, better summary.",
        model="claude-opus-4-8",
        summarized_at=datetime.now(timezone.utc),
    )
    repo.set_admission("vid_resummarized", "admitted")
    repo.commit()

    [video] = repo.list_processed_videos()
    assert video.summary == "The newer, better summary."
