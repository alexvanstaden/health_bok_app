"""Owner-driven Candidate review transitions (ADR-0004, ADR-0007, ADR-0009).

Approval must enqueue exactly one job and return immediately; rejection must take
a Candidate out of the queue without admitting anything. These assert the
queue/lifecycle effects directly against a real Postgres, no worker involved.
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok import review
from health_bok.repository import Repository
from tests.seed import seed_processed_video

VIDEO_ID = "vid_review"


def test_approve_enqueues_one_job_and_is_idempotent(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)

    assert review.approve_candidate(VIDEO_ID, repo=repo) is True
    assert repo.admission_state(VIDEO_ID) == "approved"
    assert _job_count(conn) == 1

    # A second approval (e.g. a double-click) must not enqueue a duplicate.
    assert review.approve_candidate(VIDEO_ID, repo=repo) is False
    assert _job_count(conn) == 1


def test_reject_removes_from_queue_without_admitting(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)
    review.approve_candidate(VIDEO_ID, repo=repo)  # enqueues a job

    assert review.reject_candidate(VIDEO_ID, repo=repo) is True
    assert repo.admission_state(VIDEO_ID) == "rejected"
    # The queued job is cancelled and the Candidate leaves the review queue.
    assert _queued_count(conn) == 0
    assert VIDEO_ID not in {c.video_id for c in repo.list_daily_candidates()}
    assert repo.admitted_claims(VIDEO_ID) == []


def test_plain_candidate_lists_until_acted_on(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID, summary="Zone 2 explained.")

    queue = repo.list_daily_candidates()
    assert [c.video_id for c in queue] == [VIDEO_ID]
    assert queue[0].state == "candidate"
    assert queue[0].summary == "Zone 2 explained."


def test_candidate_carries_creator_name_for_subtitle(conn):
    # The review queue shows the Creator name and publish date as subtitles
    # (issue #71), so the Candidate must carry the Creator's name.
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID, channel_name="Huberman Lab")

    queue = repo.list_daily_candidates()
    assert queue[0].creator == "Huberman Lab"


def test_daily_candidates_filter_by_processing_status(conn):
    # The review queue can be narrowed to one or more processing states (issue #75).
    # Omitting the filter lists every queue state; a selection narrows server-side.
    repo = Repository(conn)
    seed_processed_video(repo, video_id="vid_plain")
    seed_processed_video(repo, video_id="vid_failed")
    seed_processed_video(repo, video_id="vid_approved")
    repo.set_admission("vid_failed", "failed")
    repo.set_admission("vid_approved", "approved")
    repo.commit()

    def ids(**kw):
        return {c.video_id for c in repo.list_daily_candidates(**kw)}

    # Unfiltered (and the empty selection) lists every queue state.
    assert ids() == {"vid_plain", "vid_failed", "vid_approved"}
    assert ids(statuses=[]) == {"vid_plain", "vid_failed", "vid_approved"}

    # A single state narrows to just that state.
    assert ids(statuses=["failed"]) == {"vid_failed"}

    # Multiple states union.
    assert ids(statuses=["candidate", "approved"]) == {"vid_plain", "vid_approved"}

    # An unknown value can't widen the query back to admitted/rejected; with no
    # recognised value left, it falls back to the full queue rather than erroring.
    assert ids(statuses=["admitted"]) == {"vid_plain", "vid_failed", "vid_approved"}


def test_daily_candidates_filter_by_creator_date_and_search(conn):
    # The review queue narrows by Creator, publish-date range, and free-text search,
    # each optional and composing with the others via AND (issue #76). Free-text
    # matches title + creator name + the Summary body (the daily queue's description).
    repo = Repository(conn)
    seed_processed_video(
        repo,
        video_id="hub_sleep",
        channel_id="UC_hub",
        channel_name="Huberman Lab",
        title="Sleep and circadian rhythm",
        summary="How morning light sets the clock.",
        published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    seed_processed_video(
        repo,
        video_id="hub_old",
        channel_id="UC_hub",
        channel_name="Huberman Lab",
        title="Old episode on the cold",
        summary="Cold exposure basics.",
        published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    seed_processed_video(
        repo,
        video_id="att_zone2",
        channel_id="UC_att",
        channel_name="Peter Attia",
        title="Zone 2 training",
        summary="Sleep also matters for recovery.",
        published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )

    def ids(**kw):
        return {c.video_id for c in repo.list_daily_candidates(**kw)}

    # Unfiltered (and an empty selection) lists the whole queue.
    assert ids() == {"hub_sleep", "hub_old", "att_zone2"}

    # Creator narrows to the chosen channel_id(s).
    assert ids(creators=["UC_hub"]) == {"hub_sleep", "hub_old"}

    # The publish-date range is inclusive on each bound and composes either way.
    after = datetime(2026, 1, 1, tzinfo=timezone.utc)
    before = datetime(2025, 12, 31, tzinfo=timezone.utc)
    assert ids(published_from=after) == {"hub_sleep", "att_zone2"}
    assert ids(published_to=before) == {"hub_old"}

    # Free-text spans title, creator name, and the Summary body.
    assert ids(search="sleep") == {"hub_sleep", "att_zone2"}  # title + summary
    assert ids(search="attia") == {"att_zone2"}  # creator name
    assert ids(search="cold exposure") == {"hub_old"}  # summary only

    # Dimensions AND together — the result is their intersection.
    assert ids(creators=["UC_hub"], search="sleep") == {"hub_sleep"}
    assert ids(creators=["UC_hub"], published_from=after) == {"hub_sleep"}


def test_api_parses_date_bounds_inclusively():
    # The API turns `YYYY-MM-DD` query bounds into UTC datetimes for the publish-date
    # filter (issue #76): `from` anchors at the day's start, `to` at its very end so
    # the range is inclusive on both ends. Absent or unparseable input means no bound.
    from datetime import datetime, timezone

    from health_bok.api import _parse_date_bound

    assert _parse_date_bound("2026-06-20", end_of_day=False) == datetime(
        2026, 6, 20, 0, 0, 0, tzinfo=timezone.utc
    )
    assert _parse_date_bound("2026-06-20", end_of_day=True) == datetime(
        2026, 6, 20, 23, 59, 59, 999999, tzinfo=timezone.utc
    )
    assert _parse_date_bound(None, end_of_day=True) is None
    assert _parse_date_bound("not-a-date", end_of_day=False) is None


def _job_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        return cur.fetchone()[0]


def _queued_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE state = 'queued'")
        return cur.fetchone()[0]
