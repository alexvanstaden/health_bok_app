"""The background worker that drains the admission queue (ADR-0009).

Approval enqueues a job and returns; this worker does the slow part without
blocking the request. For a daily Candidate that is just extract → normalize
Concepts → admit; for a backfill Candidate it is **transcribe-if-needed first** —
a backfill Candidate is stored metadata-only, so the worker acquires its
Transcript (free captions, else paid Whisper) and archives it before extracting
(issue #15). It claims jobs one at a time with `FOR UPDATE SKIP LOCKED` (so
multiple workers are safe), walks the Candidate `approved → processing →
admitted`, and on any failure rolls the admission back and drives the Candidate to
`failed`, leaving it retryable. The job's outcome (`done`/`failed`) and the
Candidate's lifecycle state are both visible in the Web App.

Depends only on the ports, the `ConceptNormalizer`, and the repository, so fake
`ContentSource`/`Transcriber`/`Extractor`/`Embedder` over a real Postgres
exercise the whole drain in tests. The continuous polling loop lives in the
entrypoint (`main.py`); this module is the testable unit of work.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg

from . import curation, personal
from .acquire import acquire_transcript
from .admit import admit_candidate
from .concepts import ConceptNormalizer
from .impacts import detect_for_admitted_video, detect_relationship_impacts_for_video
from .ports import (
    ContentSource,
    Embedder,
    Extractor,
    HierarchyProposer,
    StanceJudge,
    Summarizer,
    Transcriber,
)
from .repository import Repository

logger = logging.getLogger("health_bok.worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def process_next_job(
    *,
    content_source: ContentSource,
    transcriber: Transcriber,
    extractor: Extractor,
    normalizer: ConceptNormalizer,
    repo: Repository,
    judge: StanceJudge | None = None,
    summarizer: Summarizer | None = None,
    hierarchy_proposer: HierarchyProposer | None = None,
    embedder: Embedder | None = None,
    embedding_model: str = "",
    model: str = "",
) -> bool:
    """Claim and run the next queued job. Returns ``False`` when none is queued.

    The claim and the `processing` mark commit together (a tiny window holding the
    job row lock), so the Candidate is visibly processing the instant the job
    leaves the queue. A backfill Candidate is then transcribed-if-needed and its
    Transcript archived in its own committed step, so a later extraction failure
    never re-transcribes (re-paying Whisper). Admission runs last in its own
    transaction: on success the job is `done`; on failure everything admission
    wrote is rolled back and the Candidate is driven to `failed` with the error
    recorded — never half-admitted, and the archived Transcript survives for retry.

    Once admission has committed, two post-admission steps run, each in its own
    failure-isolated transaction so a hiccup never undoes the durable admission
    (ADR-0005):

      * **summarize-if-missing** (issue #80) — a backfill Candidate reaches admission
        with Claims/Protocols but no Summary (it skipped the daily summarize step), so
        the worker summarizes it now, writing the same Summary artifact a daily video
        gets. A daily Candidate already has a Summary and is left untouched, never
        re-paying the cost. Skipped entirely when no `Summarizer` is wired.
      * the **Impact** forward pass over the just-admitted Claims/Protocols (issue
        #18) — only if a `StanceJudge` is wired. The daily-pipeline tests that don't
        exercise change detection leave `judge` unset, and detection is simply skipped.
    """
    job = repo.claim_next_job()
    if job is None:
        return False

    repo.set_admission(job.video_id, "processing")
    repo.commit()

    try:
        _ensure_transcript_archived(
            job.video_id,
            content_source=content_source,
            transcriber=transcriber,
            repo=repo,
        )
        admit_candidate(
            job.video_id, extractor=extractor, normalizer=normalizer, repo=repo
        )
        repo.mark_job_done(job.id)
        repo.commit()
    except Exception as exc:  # one failed admission must not poison the worker
        repo.rollback()
        repo.set_admission(job.video_id, "failed", error=str(exc))
        repo.mark_job_failed(job.id, error=str(exc))
        repo.commit()
        logger.warning("admission failed for %s: %s", job.video_id, exc)
        return True

    _summarize_if_missing(
        job.video_id, summarizer=summarizer, model=model, repo=repo
    )
    _detect_impacts(job.video_id, judge=judge, repo=repo)
    _link_hierarchy_for_video(
        job.video_id,
        proposer=hierarchy_proposer,
        embedder=embedder,
        model=embedding_model,
        repo=repo,
    )
    _match_goals_for_video(
        job.video_id, embedder=embedder, model=embedding_model, repo=repo
    )
    return True


def _summarize_if_missing(
    video_id: str,
    *,
    summarizer: Summarizer | None,
    model: str,
    repo: Repository,
) -> None:
    """Summarize a just-admitted video if it has no Summary yet, failure-isolated (#80).

    A daily Candidate was summarized before it was ever reviewed, so it already has a
    Summary: this is a no-op for it, leaving the existing Summary untouched and never
    re-paying the cost. A backfill Candidate went candidate → approve → admission,
    skipping the daily summarize step, so it reaches admission with Claims/Protocols
    but no Summary; this fills that gap, writing a `summaries` row and stamping
    `summarized_at` exactly as the daily path does — so a backfill-admitted video
    carries the same Summary artifact and shows on the Logs page with it (issue #79).

    Runs *after* admission has committed, in its own transaction: a summarize hiccup is
    logged and rolled back, never failing or undoing an otherwise-successful admission
    (ADR-0005), consistent with the post-admission Impact passes. Skipped entirely when
    no `Summarizer` is wired (the daily-pipeline tests that don't exercise it).
    """
    if summarizer is None:
        return
    try:
        if repo.get_summary(video_id) is not None:
            return  # already summarized (the daily path) — don't re-pay the cost
        transcript = repo.load_fetched_transcript(video_id)
        if transcript is None:
            return
        summary = summarizer.summarize(transcript)
        repo.save_summary(video_id, summary, model=model, summarized_at=_utcnow())
        repo.commit()
    except Exception as exc:
        repo.rollback()
        logger.warning("summarize-on-admission failed for %s: %s", video_id, exc)


def _detect_impacts(
    video_id: str, *, judge: StanceJudge | None, repo: Repository
) -> None:
    """Run the post-admission Impact passes over a just-admitted video, failure-isolated.

    Two passes, each *after* admission has committed so a failure costs only change
    detection — the Claims are durably admitted and the job is already `done`
    (ADR-0005):

      * the **relationship** pass (ADR-0013) — structural, no LLM — always runs,
        alerting on the lateral relationships the video derived (Tier-1 push to
        tracked Goals/Decisions, Tier-2 feed otherwise);
      * the **knowledge↔anchor** forward pass (issue #18) runs only when a
        `StanceJudge` is wired; the daily-pipeline tests that don't exercise it
        leave `judge` unset and it is skipped.
    """
    try:
        detect_relationship_impacts_for_video(video_id, repo=repo)
    except Exception as exc:
        repo.rollback()
        logger.warning("relationship alerting failed for %s: %s", video_id, exc)
    if judge is None:
        return
    try:
        detect_for_admitted_video(video_id, judge=judge, repo=repo)
    except Exception as exc:
        repo.rollback()
        logger.warning("impact detection failed for %s: %s", video_id, exc)


def _link_hierarchy_for_video(
    video_id: str,
    *,
    proposer: HierarchyProposer | None,
    embedder: Embedder | None,
    model: str,
    repo: Repository,
) -> None:
    """Auto-organize the taxonomy for a just-admitted video's Concepts (ADR-0014).

    The two-tier gate applied per-video, after admission has committed: for each
    Concept the video touched, propose broader parents (the same embedding-cluster +
    LLM suggester the CLI backfill uses) and, when a parent sits within
    `curation.BROADER_AUTOCONFIRM_DISTANCE`, confirm it outright so roll-up organizes
    without the owner; a looser proposal is left unconfirmed for the review queue. So
    a newly-admitted Concept finds its place in the hierarchy automatically instead of
    waiting to be curated by hand.

    Runs *after* admission has committed, failure-isolated in its own transaction: a
    proposer/embedder hiccup is logged and rolled back, never undoing the durable
    admission (ADR-0005), consistent with the other post-admission steps. Skipped
    entirely when no `HierarchyProposer`/`Embedder` is wired (the daily-pipeline tests
    that don't exercise it).
    """
    if proposer is None or embedder is None:
        return
    try:
        concept_ids = repo.concept_ids_for_video(video_id)
    except Exception as exc:
        repo.rollback()
        logger.warning("auto-hierarchy failed for %s: %s", video_id, exc)
        return
    for concept_id in concept_ids:
        try:
            suggestions = curation.suggest_broader_of(
                concept_id, proposer=proposer, embedder=embedder, repo=repo, model=model
            )
            for parent in suggestions:
                try:
                    proposed = curation.propose_broader_of(
                        parent.id, concept_id, repo=repo
                    )
                except psycopg.errors.RaiseException:
                    # The cycle-guard rejected an edge that would close a loop: roll
                    # the failed proposal back and keep going (as the backfill does).
                    repo.rollback()
                    continue
                if proposed and parent.distance <= curation.BROADER_AUTOCONFIRM_DISTANCE:
                    curation.confirm_broader_of(parent.id, concept_id, repo=repo)
        except Exception as exc:  # a proposer/embedder hiccup on one Concept
            repo.rollback()
            logger.warning(
                "auto-hierarchy failed for %s concept %s: %s", video_id, concept_id, exc
            )


def _match_goals_for_video(
    video_id: str, *, embedder: Embedder | None, model: str, repo: Repository
) -> None:
    """Auto-link a just-admitted video's Concepts to the Goals they match (ADR-0014).

    The high-confidence tier of goal-matching, after admission has committed: a
    Concept the video touched that closely matches a Goal's text is attached to it
    automatically (`personal.auto_attach_goal_concepts_for_video`), so Goals stay
    current without the owner curating; looser matches remain owner-confirmed
    suggestions on the Goal page. Failure-isolated in its own transaction — a hiccup
    is logged and rolled back, never undoing the durable admission (ADR-0005).
    Skipped entirely when no `Embedder` is wired (the daily-pipeline tests).
    """
    if embedder is None:
        return
    try:
        personal.auto_attach_goal_concepts_for_video(
            video_id, embedder=embedder, repo=repo, model=model
        )
    except Exception as exc:
        repo.rollback()
        logger.warning("goal auto-match failed for %s: %s", video_id, exc)


def _ensure_transcript_archived(
    video_id: str,
    *,
    content_source: ContentSource,
    transcriber: Transcriber,
    repo: Repository,
) -> None:
    """Make sure the Candidate has an archived Transcript before extraction.

    A daily Candidate already does, so this is a no-op for it. A backfill Candidate
    does not (it was stored metadata-only, issue #15): acquire one transcribe-if-
    needed — free captions, else Whisper (PRD #1, user stories 9-10, 29) — and
    archive it in its own committed transaction, so the immutable Transcript is
    durable the moment it is acquired and a later extraction failure retries
    extraction alone, never the (paid) transcription.
    """
    if repo.load_fetched_transcript(video_id) is not None:
        return
    fetched = acquire_transcript(
        video_id, content_source=content_source, transcriber=transcriber
    )
    repo.archive_transcript(fetched, retrieved_at=_utcnow())
    repo.commit()


def drain(
    *,
    content_source: ContentSource,
    transcriber: Transcriber,
    extractor: Extractor,
    normalizer: ConceptNormalizer,
    repo: Repository,
    judge: StanceJudge | None = None,
    summarizer: Summarizer | None = None,
    hierarchy_proposer: HierarchyProposer | None = None,
    embedder: Embedder | None = None,
    embedding_model: str = "",
    model: str = "",
) -> int:
    """Process every currently-queued job; return how many were handled."""
    handled = 0
    while process_next_job(
        content_source=content_source,
        transcriber=transcriber,
        extractor=extractor,
        normalizer=normalizer,
        repo=repo,
        judge=judge,
        summarizer=summarizer,
        hierarchy_proposer=hierarchy_proposer,
        embedder=embedder,
        embedding_model=embedding_model,
        model=model,
    ):
        handled += 1
    return handled
