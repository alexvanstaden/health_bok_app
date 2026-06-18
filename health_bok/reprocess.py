"""Retroactively establish lateral Relationships across the existing library (issue #64).

Lateral Relationships (`concept_relations`) are derived only at admit time from a
Claim's extracted predicate triples (ADR-0013). Every Claim admitted *before*
triple-aware extraction shipped therefore has none, so the owner's pre-existing
Body of Knowledge is still a pile of islands. This walks every already-admitted
video and re-extracts it from its **archived Transcript** — the source of truth
(ADR-0001) — through the existing supersede path (`supersede_claims`, ADR-0005),
which re-projects each Claim's triples into `concept_relations` with evidence links
and self-heals any relationship left unevidenced.

No YouTube re-fetch and no Whisper: only the archived Transcript is re-extracted
(a video without one is reported and skipped, never re-acquired), so this is not a
reprocess from source. The accepted side effects are a normal supersede
(ADR-0005/0010): non-protected Claims are regenerated, owner-**protected** Claims
are left untouched, and the post-admission Impact passes are deliberately *not*
run here — this is a one-off backfill of structure, not the live admission path.

Resumable and idempotent: each video's supersede and its `relationship_reprocess`
marker commit together, so an interrupted run resumes where it left off and a
second full run is a no-op — already-reprocessed videos are skipped and the
Extractor is never re-paid. Like the daily job it owns its own transaction
boundary (it commits per video); unlike the worker it isolates a single video's
failure so one bad video never aborts the whole library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .admit import supersede_claims
from .concepts import ConceptNormalizer
from .ports import Extractor
from .repository import Repository

logger = logging.getLogger("health_bok.reprocess")


@dataclass(frozen=True)
class ReprocessResult:
    """What a relationship-reprocess run did — for logging and test assertions."""

    reprocessed: list[str] = field(default_factory=list)
    skipped_no_transcript: list[str] = field(default_factory=list)
    already_done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    relations_removed: int = 0


def reprocess_relationships(
    *,
    extractor: Extractor,
    normalizer: ConceptNormalizer,
    repo: Repository,
) -> ReprocessResult:
    """Re-establish lateral Relationships across every already-admitted video.

    Walks the admitted set (oldest first), skipping any video already reprocessed
    by a prior run (resume) and any without an archived Transcript (reported, never
    re-fetched). Each remaining video is re-extracted through `supersede_claims`,
    which re-projects its triples into `concept_relations`; the supersede and its
    completion marker commit together so the run is resumable. A single video's
    failure is isolated (rolled back and recorded), leaving it for the next run.
    """
    done = repo.reprocessed_video_ids()
    reprocessed: list[str] = []
    skipped: list[str] = []
    already_done: list[str] = []
    failed: list[tuple[str, str]] = []
    relations_removed = 0

    for video_id in repo.admitted_video_ids():
        if video_id in done:
            already_done.append(video_id)
            continue
        if repo.load_fetched_transcript(video_id) is None:
            # No archived Transcript (ADR-0001): report it, never re-acquire it.
            logger.warning("skipping %s: no archived Transcript", video_id)
            skipped.append(video_id)
            continue
        try:
            outcome = supersede_claims(
                video_id, extractor=extractor, normalizer=normalizer, repo=repo
            )
            repo.mark_reprocessed(video_id)
            repo.commit()
        except Exception as exc:  # one bad video must not abort the library
            repo.rollback()
            logger.warning("reprocess failed for %s: %s", video_id, exc)
            failed.append((video_id, str(exc)))
            continue
        reprocessed.append(video_id)
        relations_removed += outcome.relations_removed

    logger.info(
        "reprocess complete: %d reprocessed, %d already done, %d skipped (no "
        "transcript), %d failed, %d relation(s) removed",
        len(reprocessed), len(already_done), len(skipped), len(failed),
        relations_removed,
    )
    return ReprocessResult(
        reprocessed=reprocessed,
        skipped_no_transcript=skipped,
        already_done=already_done,
        failed=failed,
        relations_removed=relations_removed,
    )
