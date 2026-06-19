"""Lazy Candidate detail fetch + publish-date sort (issue #31 / slice 31).

The cheap one-pass backfill listing (user story 29) lists a back-catalogue without
per-video descriptions and with only a best-effort publish date. This slice adds a
per-Candidate "fetch details" that recovers the *real* description and the accurate
publish date on demand and persists both, plus a publish-date sort on the queue.

Drives the new adapter parsing, the persistence + service path, and the sort against
a faked ContentSource plus a real ephemeral Postgres, asserting on what gets stored
and read back — the existing port + real-Postgres style (PRD #1 testing decisions).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from health_bok import backfill
from health_bok.adapters.youtube import _candidate_details
from health_bok.models import CandidateDetails, CandidateMetadata, CreatorIdentity
from health_bok.repository import Repository
from tests.fakes import FakeContentSource

HUBERMAN = CreatorIdentity(channel_id="UC2D2CMWXMOVWx7giW1n3LIg", name="Huberman Lab")
NOW = datetime(2026, 6, 9, tzinfo=timezone.utc)


def _at(days_ago: int) -> datetime:
    return NOW - timedelta(days=days_ago)


def _seed_candidate(conn, video_id: str, *, days_ago: int = 10, **kw) -> int:
    """HUBERMAN on the watch list with one metadata-only backfill Candidate."""
    repo = Repository(conn)
    creator_id = repo.add_creator(HUBERMAN)
    repo.add_candidate(
        creator_id,
        CandidateMetadata(
            video_id=video_id,
            title=kw.get("title", f"Episode {video_id}"),
            description=kw.get("description", ""),  # listing leaves it empty
            published_at=_at(days_ago),
        ),
    )
    repo.commit()
    return creator_id


# == The adapter's per-video detail parsing (issue #31) =====================


def test_adapter_parses_description_and_accurate_publish_date():
    # A full (non-flat) extraction carries the per-video description and a timestamp,
    # so the publish date is the accurate one — not the listing's best-effort guess.
    info = {
        "id": "abc123",
        "description": "Morning sunlight and circadian rhythm.",
        "timestamp": int(_at(40).timestamp()),
        "upload_date": "20990101",  # timestamp wins over upload_date
    }

    details = _candidate_details(info)

    assert details == CandidateDetails(
        description="Morning sunlight and circadian rhythm.",
        published_at=_at(40),
    )


def test_adapter_falls_back_to_upload_date_when_no_timestamp():
    info = {"id": "abc123", "description": "Notes.", "upload_date": "20260501"}

    details = _candidate_details(info)

    assert details.description == "Notes."
    assert details.published_at == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_adapter_tolerates_a_missing_description():
    # A genuinely description-less video parses to an empty string, never None.
    details = _candidate_details({"id": "abc123", "upload_date": "20260501"})

    assert details.description == ""


# == Fetch persists the real description + accurate date (issue #31) =========


def test_fetch_details_persists_description_and_corrected_date(conn):
    _seed_candidate(conn, "vid1", days_ago=10)  # empty description, rough date
    source = FakeContentSource(
        details={
            "vid1": CandidateDetails(
                description="The real, fetched description.",
                published_at=_at(42),
            )
        }
    )

    updated = backfill.fetch_candidate_details(
        "vid1", content_source=source, repo=Repository(conn)
    )

    # The per-video extraction ran exactly once for this Candidate (AC: one call).
    assert source.details_fetched == ["vid1"]
    # The returned Candidate carries the fetched detail in place...
    assert updated is not None
    assert updated.description == "The real, fetched description."
    assert updated.published_at == _at(42)
    # ...and a fresh read shows it persisted (a re-load still shows them).
    (stored,) = Repository(conn).list_backfill_candidates()
    assert stored.description == "The real, fetched description."
    assert stored.published_at == _at(42)


def test_fetch_details_is_idempotent_and_updates_not_duplicates(conn):
    _seed_candidate(conn, "vid1", days_ago=10)
    repo = Repository(conn)

    first = CandidateDetails(description="first pass", published_at=_at(30))
    backfill.fetch_candidate_details(
        "vid1", content_source=FakeContentSource(details={"vid1": first}), repo=repo
    )
    second = CandidateDetails(description="second pass", published_at=_at(31))
    backfill.fetch_candidate_details(
        "vid1", content_source=FakeContentSource(details={"vid1": second}), repo=repo
    )

    # Re-running updates the one row in place — never a duplicate Candidate (AC 5).
    candidates = Repository(conn).list_backfill_candidates()
    assert [c.video_id for c in candidates] == ["vid1"]
    assert candidates[0].description == "second pass"
    assert candidates[0].published_at == _at(31)


def test_fetch_details_for_unknown_candidate_returns_none_without_fetching(conn):
    Repository(conn).add_creator(HUBERMAN)  # a Creator, but no such Candidate
    source = FakeContentSource()

    result = backfill.fetch_candidate_details(
        "ghost", content_source=source, repo=Repository(conn)
    )

    assert result is None
    # No Candidate → the expensive per-video fetch is skipped entirely.
    assert source.details_fetched == []


# == Sort the queue by publish date (issue #31) =============================


def test_backfill_queue_sorts_by_publish_date_both_ways(conn):
    repo = Repository(conn)
    creator_id = repo.add_creator(HUBERMAN)
    for vid, days_ago in (("old", 100), ("newest", 5), ("middle", 40)):
        repo.add_candidate(
            creator_id,
            CandidateMetadata(
                video_id=vid, title=vid, description="", published_at=_at(days_ago)
            ),
        )
    repo.commit()

    newest_first = Repository(conn).list_backfill_candidates()  # default
    assert [c.video_id for c in newest_first] == ["newest", "middle", "old"]

    oldest_first = Repository(conn).list_backfill_candidates(newest_first=False)
    assert [c.video_id for c in oldest_first] == ["old", "middle", "newest"]


# == Filter the queue by processing status (issue #75) ======================


def test_backfill_queue_filter_by_processing_status(conn):
    # The backfill queue narrows to one or more processing states, and the filter
    # composes with the existing newest/oldest sort (issue #75).
    repo = Repository(conn)
    creator_id = repo.add_creator(HUBERMAN)
    for vid, days_ago in (("plain", 5), ("processing", 40), ("failed", 100)):
        repo.add_candidate(
            creator_id,
            CandidateMetadata(
                video_id=vid, title=vid, description="", published_at=_at(days_ago)
            ),
        )
    repo.set_admission("processing", "processing")
    repo.set_admission("failed", "failed")
    repo.commit()

    def ids(**kw):
        return {c.video_id for c in Repository(conn).list_backfill_candidates(**kw)}

    # Unfiltered (and the empty selection) lists every queue state.
    assert ids() == {"plain", "processing", "failed"}
    assert ids(statuses=[]) == {"plain", "processing", "failed"}

    # A single state narrows; multiple states union.
    assert ids(statuses=["failed"]) == {"failed"}
    assert ids(statuses=["candidate", "processing"]) == {"plain", "processing"}

    # Filter composes with sort: oldest-first within the narrowed set.
    narrowed = Repository(conn).list_backfill_candidates(
        newest_first=False, statuses=["candidate", "failed"]
    )
    assert [c.video_id for c in narrowed] == ["failed", "plain"]


# == Filter by Creator, date range, and free-text search (issue #76) ========


def test_backfill_queue_filter_by_creator_date_and_search(conn):
    # The backfill queue narrows by Creator, publish-date range, and free-text
    # search, each optional and composing via AND (issue #76). Free-text matches
    # title + creator name + the real description.
    repo = Repository(conn)
    hub = repo.add_creator(HUBERMAN)
    attia = repo.add_creator(CreatorIdentity(channel_id="UCattia", name="Peter Attia"))

    def add(creator_id, video_id, title, description, days_ago):
        repo.add_candidate(
            creator_id,
            CandidateMetadata(
                video_id=video_id,
                title=title,
                description=description,
                published_at=_at(days_ago),
            ),
        )

    add(hub, "hub_sleep", "Sleep and light", "Morning light and circadian rhythm.", 10)
    add(hub, "hub_old", "Cold plunge basics", "Cold exposure protocol.", 400)
    add(attia, "att_zone2", "Zone 2 training", "Why sleep aids recovery.", 5)
    repo.commit()

    def ids(**kw):
        return {c.video_id for c in Repository(conn).list_backfill_candidates(**kw)}

    # Unfiltered lists the whole queue.
    assert ids() == {"hub_sleep", "hub_old", "att_zone2"}

    # Creator narrows by channel_id.
    assert ids(creators=[HUBERMAN.channel_id]) == {"hub_sleep", "hub_old"}

    # The publish-date range is inclusive on each bound.
    assert ids(published_from=_at(30)) == {"hub_sleep", "att_zone2"}
    assert ids(published_to=_at(30)) == {"hub_old"}

    # Free-text spans title, creator name, and description.
    assert ids(search="sleep") == {"hub_sleep", "att_zone2"}  # title + description
    assert ids(search="attia") == {"att_zone2"}  # creator name
    assert ids(search="cold exposure") == {"hub_old"}  # description only

    # Dimensions AND together — the result is their intersection.
    assert ids(creators=[HUBERMAN.channel_id], search="sleep") == {"hub_sleep"}
