"""Owner-driven Candidate review transitions (ADR-0004, ADR-0007, ADR-0009).

Approval must enqueue exactly one job and return immediately; rejection must take
a Candidate out of the queue without admitting anything. These assert the
queue/lifecycle effects directly against a real Postgres, no worker involved.
"""

from __future__ import annotations

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


def _job_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        return cur.fetchone()[0]


def _queued_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE state = 'queued'")
        return cur.fetchone()[0]
