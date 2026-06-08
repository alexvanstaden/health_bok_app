"""The daily job orchestrator.

Slice 1 drives one known video through every layer: fetch its Transcript via the
`ContentSource` port, archive it immutably with provenance, summarize it via the
`Summarizer` port, persist the Summary, and send a one-item Digest via the
`DigestSender` port. The store (Postgres) is real, reached through `Repository`.

The orchestrator depends only on the port protocols and the repository, so it
imports no third-party SDK and can be driven in tests with fakes (PRD #1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Digest, DigestItem
from .ports import ContentSource, DigestSender, Summarizer
from .repository import Repository


@dataclass(frozen=True)
class RunResult:
    """What the run did — for logging and for tests to assert on."""

    newly_processed: list[str] = field(default_factory=list)
    digest_sent: bool = False
    digest_item_count: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_job(
    video_id: str,
    *,
    content_source: ContentSource,
    summarizer: Summarizer,
    digest_sender: DigestSender,
    repo: Repository,
    model: str,
    now=_utcnow,
) -> RunResult:
    """Run the slice-1 pipeline for a single video.

    Idempotent: a video already processed is not re-fetched or re-summarized, and
    a Digest already sent for it is not sent again (user stories 22-24). A failed
    Digest send leaves the video processed but unsent, so a later run retries the
    send without re-summarizing.
    """
    newly_processed: list[str] = []

    if not repo.is_processed(video_id):
        retrieved_at = now()
        fetched = content_source.fetch_transcript(video_id)
        repo.archive_transcript(fetched, retrieved_at=retrieved_at)
        summary = summarizer.summarize(fetched)
        repo.save_summary(video_id, summary, model=model, summarized_at=now())
        # Commit the Transcript + Summary together before touching email, so the
        # video is durably "processed" even if the Digest send later fails.
        repo.commit()
        newly_processed.append(video_id)

    # Assemble the Digest from processed videos whose Summary has not yet gone
    # out. In slice 1 that is at most the one video.
    pending = [video_id] if not repo.digest_already_sent(video_id) else []

    items: list[DigestItem] = []
    for vid in pending:
        archived = repo.get_summary(vid)
        if archived is not None:
            items.append(
                DigestItem(title=archived.title, url=archived.url, summary=archived.body)
            )

    digest = Digest(items=items)
    if digest.is_empty:
        # No new content -> no Digest is sent (user story 19).
        return RunResult(newly_processed=newly_processed, digest_sent=False)

    # If the send raises, digest_sent stays unrecorded, so a later run retries
    # the send without re-summarizing (user story 24).
    digest_sender.send(digest)
    repo.mark_digest_sent(pending, sent_at=now())
    repo.commit()

    return RunResult(
        newly_processed=newly_processed,
        digest_sent=True,
        digest_item_count=len(items),
    )
