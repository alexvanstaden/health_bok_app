"""Creator-management tests (issue #3 / PRD #1 user stories 1-4).

Drives the add/remove service against a faked ContentSource for @handle
resolution plus a real ephemeral Postgres, asserting on what gets persisted —
the watch list — never on internal details (PRD #1 testing decisions).
"""

from __future__ import annotations

import pytest

from health_bok import creators
from health_bok.models import CreatorIdentity, CreatorResolutionError
from health_bok.repository import Repository
from tests.fakes import FakeContentSource

HUBERMAN = CreatorIdentity(channel_id="UC2D2CMWXMOVWx7giW1n3LIg", name="Huberman Lab")
ATTIA = CreatorIdentity(channel_id="UC8kGsMa0LygSlsDfASTbjBA", name="Peter Attia MD")

# Two distinct references — an @handle and a full channel URL — that the owner
# might use for the same Creator.
HANDLE = "@hubermanlab"
URL = "https://www.youtube.com/@hubermanlab"


def _source(**identities: CreatorIdentity) -> FakeContentSource:
    """A ContentSource that resolves each given reference to its identity."""
    return FakeContentSource(identities=identities)


def test_add_by_handle_resolves_once_and_stores_stable_identity(conn):
    source = _source(**{HANDLE: HUBERMAN})
    repo = Repository(conn)

    identity = creators.add_creator(HANDLE, content_source=source, repo=repo)

    # The @handle is resolved exactly once (AC 2)...
    assert source.resolved == [HANDLE]
    assert identity == HUBERMAN
    # ...and the resolved stable identity is what gets persisted (AC 3).
    assert Repository(conn).list_creators() == [HUBERMAN]


def test_add_by_url_is_supported(conn):
    source = _source(**{URL: HUBERMAN})
    repo = Repository(conn)

    creators.add_creator(URL, content_source=source, repo=repo)

    assert source.resolved == [URL]
    assert Repository(conn).list_creators() == [HUBERMAN]


def test_re_adding_same_handle_does_not_duplicate(conn):
    source = _source(**{HANDLE: HUBERMAN})
    repo = Repository(conn)

    creators.add_creator(HANDLE, content_source=source, repo=repo)
    creators.add_creator(HANDLE, content_source=source, repo=repo)

    # Idempotent on channel_id — one row, not two (AC 4).
    assert Repository(conn).list_creators() == [HUBERMAN]


def test_handle_and_url_for_same_channel_dedupe_on_channel_id(conn):
    # The owner adds the same Creator two ways; both resolve to one channel_id,
    # so the watch list holds a single, stable identity (AC 3, AC 4).
    source = _source(**{HANDLE: HUBERMAN, URL: HUBERMAN})
    repo = Repository(conn)

    creators.add_creator(HANDLE, content_source=source, repo=repo)
    creators.add_creator(URL, content_source=source, repo=repo)

    assert Repository(conn).list_creators() == [HUBERMAN]


def test_re_adding_refreshes_the_display_name(conn):
    renamed = CreatorIdentity(channel_id=HUBERMAN.channel_id, name="Huberman Lab Clips")
    source = _source(**{HANDLE: HUBERMAN, "@huberman2": renamed})
    repo = Repository(conn)

    creators.add_creator(HANDLE, content_source=source, repo=repo)
    creators.add_creator("@huberman2", content_source=source, repo=repo)

    # Same channel_id, name updated, still one row.
    assert Repository(conn).list_creators() == [renamed]


def test_add_then_remove_by_channel_id(conn):
    source = _source(**{HANDLE: HUBERMAN, "@peterattia": ATTIA})
    repo = Repository(conn)
    creators.add_creator(HANDLE, content_source=source, repo=repo)
    creators.add_creator("@peterattia", content_source=source, repo=repo)

    removed = creators.remove_creator(HUBERMAN.channel_id, repo=repo)

    assert removed is True
    # The other Creator is untouched (AC 1).
    assert Repository(conn).list_creators() == [ATTIA]


def test_remove_unknown_channel_id_returns_false(conn):
    repo = Repository(conn)
    assert creators.remove_creator("UCdoes-not-exist", repo=repo) is False


def test_unresolvable_reference_raises_and_persists_nothing(conn):
    source = _source(**{HANDLE: HUBERMAN})
    repo = Repository(conn)

    with pytest.raises(CreatorResolutionError):
        creators.add_creator("@typo", content_source=source, repo=repo)

    assert Repository(conn).list_creators() == []


def test_list_creators_returns_oldest_first(conn):
    source = _source(**{HANDLE: HUBERMAN, "@peterattia": ATTIA})
    repo = Repository(conn)
    creators.add_creator(HANDLE, content_source=source, repo=repo)
    creators.add_creator("@peterattia", content_source=source, repo=repo)

    assert Repository(conn).list_creators() == [HUBERMAN, ATTIA]


def _trust_tier(conn, channel_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT trust_tier FROM creators WHERE channel_id = %s", (channel_id,))
        return cur.fetchone()[0]


def test_new_creator_defaults_to_trust_tier_one(conn):
    # Issue #49 AC: a Creator is untiered until the owner says otherwise, and untiered
    # means tier 1 — so Strength degrades to a plain distinct-Creator count (ADR-0013).
    source = _source(**{HANDLE: HUBERMAN})
    repo = Repository(conn)
    creators.add_creator(HANDLE, content_source=source, repo=repo)

    assert _trust_tier(conn, HUBERMAN.channel_id) == 1


def test_set_trust_tier_persists_for_a_known_creator(conn):
    # Issue #49 AC: the owner can set a Creator's trust-tier (the "control"). The
    # write needs a commit, mirroring how the API endpoint drives it.
    source = _source(**{HANDLE: HUBERMAN})
    repo = Repository(conn)
    creators.add_creator(HANDLE, content_source=source, repo=repo)

    assert repo.set_creator_trust_tier(repo.creator_id(HUBERMAN.channel_id), 4) is True
    repo.commit()

    assert _trust_tier(conn, HUBERMAN.channel_id) == 4


def test_set_trust_tier_on_unknown_creator_returns_false(conn):
    # The API maps this False to a 404 rather than silently no-op'ing.
    assert Repository(conn).set_creator_trust_tier(999999, 3) is False
