"""Entrypoint: the `health-bok` command-line interface.

Responsibilities behind this CLI (a Python CLI is an ops/admin convenience, not a
product surface — the Web App is the product, ADR-0009):

  * ``health-bok run`` (the default) wires the real adapters to the orchestrator
    and runs the daily pipeline: for every watched Creator it detects new uploads
    via RSS, summarizes them, and — unless email is off — sends one Digest that
    deep-links into the Web App (ADR-0007).
  * ``health-bok worker`` drains the Postgres-backed admission queue (ADR-0009):
    extract → normalize Concepts → admit, walking each approved Candidate to
    `admitted` (or `failed`, retryable). The docker `worker` service runs this.
  * ``health-bok creators add|remove|list`` maintains the watch list of
    Creators, resolving an @handle/URL to a stable channel_id once at add-time.
  * ``health-bok reprocess-relationships`` is a one-off backfill (issue #64): it
    re-extracts every already-admitted video from its archived Transcript through
    the supersede path so the pre-existing library gains lateral Relationships. No
    YouTube/Whisper; resumable and idempotent. Needs the LLM + DB secrets.
  * ``health-bok hierarchy auto|propose|export|apply`` curates the owner-curated
    `broader-of` taxonomy (issue #65, ADR-0014): `auto` proposes broader parents
    across the catalogue and confirms the confident ones outright (leaving the rest
    for review); `propose`/`export`/`apply` are the no-UI CSV round-trip — propose,
    export to a CSV the owner edits, apply the edits (confirm/reject/repick).
    `auto`/`propose` need the LLM + DB secrets;
    `export`/`apply` need only the DB.
  * ``health-bok concepts dedup`` collapses near-duplicate Concepts across the
    catalogue (ADR-0014). Idempotent; needs the LLM + DB secrets.
  * ``health-bok goals backfill`` auto-attaches every Goal to the Concepts it
    closely matches, catalogue-wide (ADR-0014) — the rerunnable counterpart to the
    per-video goal-matching the worker runs. Idempotent; needs the embedding + DB
    secrets.

Creator management needs only ``DATABASE_URL`` plus the YouTube adapter, so it
never requires the Digest or LLM secrets to be set.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import config, creators, curation, dedup, hierarchy_backfill, llm, personal
from .adapters.adjudicator import ChatAdjudicator
from .adapters.embedder import OpenAIEmbedder
from .adapters.extractor import ChatExtractor
from .adapters.hierarchy_proposer import ChatHierarchyProposer
from .adapters.resend import ResendDigestSender
from .adapters.stance import ChatStanceJudge
from .adapters.summarize import ChatSummarizer
from .adapters.whisper import WhisperTranscriber
from .adapters.youtube import YouTubeContentSource
from .concepts import ConceptNormalizer
from .config import Config
from .db import connect, init_schema
from .job import run_job
from .models import CreatorResolutionError
from .reprocess import reprocess_relationships
from .repository import Repository
from .summarizer import MapReduceSummarizer
from .worker import drain

logger = logging.getLogger("health_bok")

# How long the worker waits before re-polling an empty admission queue.
DEFAULT_WORKER_POLL_SECONDS = 5


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _build_parser().parse_args(argv)
    return args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="health-bok")
    parser.set_defaults(handler=_cmd_run)  # bare `health-bok` runs the daily job
    sub = parser.add_subparsers()

    run_p = sub.add_parser("run", help="Run the daily pipeline (the default).")
    run_p.set_defaults(handler=_cmd_run)

    worker_p = sub.add_parser("worker", help="Drain the admission queue (Part 2).")
    worker_p.add_argument(
        "--once", action="store_true",
        help="Drain the queue once and exit, instead of polling forever.",
    )
    worker_p.add_argument(
        "--interval", type=int, default=DEFAULT_WORKER_POLL_SECONDS,
        help="Seconds to wait before re-polling an empty queue (default 5).",
    )
    worker_p.set_defaults(handler=_cmd_worker)

    reprocess_p = sub.add_parser(
        "reprocess-relationships",
        help="Re-establish lateral Relationships across the existing library (#64).",
    )
    reprocess_p.set_defaults(handler=_cmd_reprocess_relationships)

    hierarchy_p = sub.add_parser(
        "hierarchy", help="Curate the broader-of taxonomy via a CSV round-trip (#65)."
    )
    hierarchy_p.set_defaults(
        handler=lambda a: hierarchy_p.error("a subcommand is required")
    )
    hsub = hierarchy_p.add_subparsers()

    propose_p = hsub.add_parser(
        "propose", help="Propose broader-of parents across every existing Concept."
    )
    propose_p.set_defaults(handler=_cmd_hierarchy_propose)

    auto_p = hsub.add_parser(
        "auto",
        help="Propose parents and auto-confirm the confident ones (two-tier, #ADR-0014).",
    )
    auto_p.set_defaults(handler=_cmd_hierarchy_auto)

    export_p = hsub.add_parser(
        "export", help="Export the current proposals to a CSV the owner edits."
    )
    export_p.add_argument("path", help="Where to write the proposals CSV.")
    export_p.set_defaults(handler=_cmd_hierarchy_export)

    apply_p = hsub.add_parser(
        "apply", help="Apply an edited proposals CSV (confirm/reject/repick)."
    )
    apply_p.add_argument("path", help="The edited proposals CSV to apply.")
    apply_p.set_defaults(handler=_cmd_hierarchy_apply)

    concepts_p = sub.add_parser("concepts", help="Maintain the Concept catalogue.")
    concepts_p.set_defaults(handler=lambda a: concepts_p.error("a subcommand is required"))
    ccsub = concepts_p.add_subparsers()

    dedup_p = ccsub.add_parser(
        "dedup", help="Merge near-duplicate Concepts across the catalogue (#ADR-0014)."
    )
    dedup_p.set_defaults(handler=_cmd_concepts_dedup)

    goals_p = sub.add_parser("goals", help="Maintain the personal Goals layer.")
    goals_p.set_defaults(handler=lambda a: goals_p.error("a subcommand is required"))
    gsub = goals_p.add_subparsers()

    goals_backfill_p = gsub.add_parser(
        "backfill",
        help="Auto-attach Goals to their matching Concepts, catalogue-wide (ADR-0014).",
    )
    goals_backfill_p.set_defaults(handler=_cmd_goals_backfill)

    creators_p = sub.add_parser("creators", help="Manage the watched Creators.")
    creators_p.set_defaults(handler=lambda a: creators_p.error("a subcommand is required"))
    csub = creators_p.add_subparsers()

    add_p = csub.add_parser("add", help="Add a Creator by @handle or URL.")
    add_p.add_argument("reference", help="An @handle, bare handle, or channel URL.")
    add_p.set_defaults(handler=_cmd_creator_add)

    remove_p = csub.add_parser("remove", help="Remove a Creator by channel_id.")
    remove_p.add_argument("channel_id", help="The channel_id shown by `creators list`.")
    remove_p.set_defaults(handler=_cmd_creator_remove)

    list_p = csub.add_parser("list", help="List the watched Creators.")
    list_p.set_defaults(handler=_cmd_creator_list)

    return parser


def _cmd_run(_args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    conn = connect(cfg.database_url)
    try:
        init_schema(conn)
        repo = Repository(conn)
        # Email is only a notification (ADR-0007): with it off, no Resend sender
        # is built and the run still archives + summarizes everything.
        digest_sender = (
            ResendDigestSender(cfg.resend_api_key, cfg.digest_from, cfg.digest_recipient)
            if cfg.digest_enabled
            else None
        )
        result = run_job(
            content_source=YouTubeContentSource(),
            transcriber=WhisperTranscriber(cfg.openai_api_key),
            # Long Transcripts are map-reduced; short ones summarize in one pass.
            # The summarizer runs on whichever provider LLM_PROVIDER selects.
            summarizer=MapReduceSummarizer(
                ChatSummarizer(llm.chat_model(cfg.summary_model)),
                max_chars=cfg.summary_max_chars,
                chunk_chars=cfg.summary_chunk_chars,
            ),
            digest_sender=digest_sender,
            repo=repo,
            model=cfg.summary_model,
            send_digest=cfg.digest_enabled,
            webapp_base_url=cfg.webapp_base_url,
            # Also ingest the one-off "Process me" playlist when configured (issue
            # #69); empty means no playlist is read and the run is unchanged.
            process_me_playlist_id=cfg.process_me_playlist_id,
        )
    finally:
        conn.close()

    logger.info(
        "run complete: newly_processed=%s digest_sent=%s items=%s failures=%s",
        result.newly_processed,
        result.digest_sent,
        result.digest_item_count,
        len(result.failures),
    )
    for failure in result.failures:
        logger.warning("isolated failure: %s -> %s", failure.scope, failure.error)
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    """Drain the admission queue: extract → normalize → admit (ADR-0009, ADR-0010).

    Builds the real Extractor (on the configured LLM provider, ADR-0012) and
    Embedder (OpenAI) behind their ports, plus the map-reduce Summarizer the
    summarize-on-admission step needs (issue #80), and runs the worker. Polls forever
    by default — the docker `worker` service — or drains once with ``--once`` for an
    ops nudge. Needs the LLM and DB secrets but never the Digest ones.
    """
    conn = connect(config.database_url())
    try:
        init_schema(conn)
        repo = Repository(conn)
        embedding_model = config.embedding_model()
        embedder = OpenAIEmbedder(config.openai_api_key(), embedding_model)
        # The LLM adjudicator decides the near-match band (0.15–0.30) the embedding is
        # unsure about, so near-duplicate Concepts merge at admit time instead of
        # minting a new hub (ADR-0014). Conservative — merges only clear synonyms.
        normalizer = ConceptNormalizer(
            embedder,
            repo,
            model=embedding_model,
            merge_distance=config.concept_merge_distance(),
            adjudicator=ChatAdjudicator(llm.chat_model(config.adjudication_model())),
        )
        extractor = ChatExtractor(llm.chat_model(config.extraction_model()))
        # After admission, the forward Impact pass judges the new Claims/Protocols
        # against the owner's anchors (issue #18) — failure-isolated, so a judge
        # hiccup never undoes an admission.
        judge = ChatStanceJudge(llm.chat_model(config.stance_model()))
        # Also after admission, auto-organize the taxonomy: propose broader parents for
        # the Concepts the video touched and confirm the confident ones (ADR-0014),
        # reusing the HierarchyProposer the CLI backfill uses. Failure-isolated.
        hierarchy_proposer = ChatHierarchyProposer(
            llm.chat_model(config.hierarchy_proposal_model())
        )
        # A backfill Candidate is admitted without ever passing the daily summarize
        # step, so the worker summarizes-if-missing on admission (issue #80), using the
        # same map-reduce Summarizer the daily job does. A daily Candidate already has
        # a Summary and is left untouched. Failure-isolated, so a summarize hiccup
        # never undoes a durable admission.
        summary_model = config.summary_model()
        summarizer = MapReduceSummarizer(
            ChatSummarizer(llm.chat_model(summary_model)),
            max_chars=config.summary_max_chars(),
            chunk_chars=config.summary_chunk_chars(),
        )
        # A backfill Candidate has no archived Transcript, so the worker acquires
        # one transcribe-if-needed before extracting (issue #15): YouTube captions,
        # else Whisper (reusing the same OpenAI key the embedder needs).
        content_source = YouTubeContentSource()
        transcriber = WhisperTranscriber(config.openai_api_key())
        logger.info("worker started (poll every %ss)", args.interval)
        while True:
            handled = drain(
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
                model=summary_model,
            )
            if handled:
                logger.info("worker drained %d job(s)", handled)
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        conn.close()
    return 0


def _cmd_reprocess_relationships(_args: argparse.Namespace) -> int:
    """Backfill lateral Relationships across the existing library (issue #64).

    Builds only the Extractor (on the configured LLM provider, ADR-0012) and the
    Embedder-backed `ConceptNormalizer` the re-extraction needs — no YouTube,
    Whisper, or Digest, since only archived Transcripts are re-extracted. The run
    is resumable and idempotent (see `reprocess.reprocess_relationships`), so it is
    safe to re-run after an interruption. Needs the LLM + DB secrets.
    """
    conn = connect(config.database_url())
    try:
        init_schema(conn)
        repo = Repository(conn)
        normalizer = ConceptNormalizer(
            OpenAIEmbedder(config.openai_api_key(), config.embedding_model()),
            repo,
            model=config.embedding_model(),
            merge_distance=config.concept_merge_distance(),
        )
        extractor = ChatExtractor(llm.chat_model(config.extraction_model()))
        result = reprocess_relationships(
            extractor=extractor, normalizer=normalizer, repo=repo
        )
    finally:
        conn.close()

    logger.info(
        "reprocess-relationships done: reprocessed=%d already_done=%d "
        "skipped_no_transcript=%d failed=%d relations_removed=%d",
        len(result.reprocessed), len(result.already_done),
        len(result.skipped_no_transcript), len(result.failed),
        result.relations_removed,
    )
    for video_id, error in result.failed:
        logger.warning("reprocess failure: %s -> %s", video_id, error)
    return 1 if result.failed else 0


def _cmd_hierarchy_propose(_args: argparse.Namespace) -> int:
    """Propose broader-of parents across the existing catalogue (issue #65).

    Builds the real HierarchyProposer (on the configured LLM provider, ADR-0012) and
    the Embedder its pgvector retrieval needs, then persists every suggestion as an
    *unconfirmed* proposal. Idempotent — safe to re-run. Needs the LLM + DB secrets.
    """
    conn = connect(config.database_url())
    try:
        init_schema(conn)
        repo = Repository(conn)
        result = hierarchy_backfill.propose_all(
            proposer=ChatHierarchyProposer(
                llm.chat_model(config.hierarchy_proposal_model())
            ),
            embedder=OpenAIEmbedder(config.openai_api_key(), config.embedding_model()),
            repo=repo,
            model=config.embedding_model(),
        )
    finally:
        conn.close()
    logger.info(
        "hierarchy propose done: scanned=%d proposed=%d skipped_cycle=%d",
        result.concepts_scanned, len(result.proposed), len(result.skipped_cycle),
    )
    return 0


def _cmd_hierarchy_auto(_args: argparse.Namespace) -> int:
    """Auto-organize the taxonomy: propose parents, confirm the confident ones (ADR-0014).

    The two-tier variant of `hierarchy propose`. Runs the same per-Concept suggester
    over the whole catalogue, but confirms outright any proposal whose parent sits
    within `curation.BROADER_AUTOCONFIRM_DISTANCE` (high-confidence tier → visible to
    roll-up immediately) and leaves looser proposals unconfirmed for the review queue
    (or the export/apply CSV). This is what retro-organizes the existing orphans in
    one run. Idempotent — safe to re-run. Needs the LLM + DB secrets.
    """
    conn = connect(config.database_url())
    try:
        init_schema(conn)
        repo = Repository(conn)
        result = hierarchy_backfill.propose_all(
            proposer=ChatHierarchyProposer(
                llm.chat_model(config.hierarchy_proposal_model())
            ),
            embedder=OpenAIEmbedder(config.openai_api_key(), config.embedding_model()),
            repo=repo,
            model=config.embedding_model(),
            auto_confirm_distance=curation.BROADER_AUTOCONFIRM_DISTANCE,
        )
    finally:
        conn.close()
    logger.info(
        "hierarchy auto done: scanned=%d proposed=%d auto_confirmed=%d skipped_cycle=%d",
        result.concepts_scanned, len(result.proposed),
        len(result.auto_confirmed), len(result.skipped_cycle),
    )
    return 0


def _cmd_hierarchy_export(args: argparse.Namespace) -> int:
    """Write the current proposals to a CSV the owner edits (issue #65). DB only."""
    conn = connect(config.database_url())
    try:
        init_schema(conn)
        repo = Repository(conn)
        with open(args.path, "w", newline="", encoding="utf-8") as out:
            count = hierarchy_backfill.export_proposals(repo, out=out)
    finally:
        conn.close()
    logger.info("hierarchy export done: wrote %d proposal(s) to %s", count, args.path)
    return 0


def _cmd_hierarchy_apply(args: argparse.Namespace) -> int:
    """Apply an edited proposals CSV — confirm/reject/repick (issue #65). DB only."""
    conn = connect(config.database_url())
    try:
        init_schema(conn)
        repo = Repository(conn)
        with open(args.path, newline="", encoding="utf-8") as source:
            result = hierarchy_backfill.apply_decisions(repo, source=source)
    finally:
        conn.close()
    logger.info(
        "hierarchy apply done: confirmed=%d rejected=%d repicked=%d "
        "skipped_cycle=%d skipped_missing=%d unchanged=%d",
        len(result.confirmed), len(result.rejected), len(result.repicked),
        len(result.skipped_cycle), len(result.skipped_missing), result.unchanged,
    )
    return 0


def _cmd_concepts_dedup(_args: argparse.Namespace) -> int:
    """Collapse near-duplicate Concepts across the catalogue (ADR-0014).

    Builds the Embedder its pgvector retrieval needs and the LLM Adjudicator that
    decides the near-match band, then merges confidently-duplicate hubs, keeping the
    canonical one and re-pointing everything onto it. Idempotent and resumable — safe
    to re-run. Needs the LLM + DB secrets.
    """
    conn = connect(config.database_url())
    try:
        init_schema(conn)
        repo = Repository(conn)
        result = dedup.dedup_catalogue(
            embedder=OpenAIEmbedder(config.openai_api_key(), config.embedding_model()),
            repo=repo,
            model=config.embedding_model(),
            adjudicator=ChatAdjudicator(llm.chat_model(config.adjudication_model())),
        )
    finally:
        conn.close()
    logger.info(
        "concepts dedup done: scanned=%d merged=%d reviewed=%d skipped_cycle=%d",
        result.concepts_scanned, len(result.merged),
        len(result.reviewed_not_merged), len(result.skipped_cycle),
    )
    return 0


def _cmd_goals_backfill(_args: argparse.Namespace) -> int:
    """Auto-attach every Goal to the Concepts it closely matches, catalogue-wide (ADR-0014).

    The rerunnable backfill counterpart to the per-video goal-matching the worker runs
    after admission: matches each Goal's text against the whole Concept catalogue over
    pgvector and attaches those within `personal.GOAL_AUTOATTACH_DISTANCE` not already
    linked. Use it to bring existing Goals current after seeding the catalogue or after
    raising the cutoff. Idempotent — safe to re-run. Needs the embedding + DB secrets.
    """
    conn = connect(config.database_url())
    try:
        init_schema(conn)
        repo = Repository(conn)
        result = personal.auto_attach_goal_concepts(
            embedder=OpenAIEmbedder(config.openai_api_key(), config.embedding_model()),
            repo=repo,
            model=config.embedding_model(),
        )
    finally:
        conn.close()
    logger.info(
        "goals backfill done: scanned=%d attached=%d",
        result.goals_scanned, len(result.attached),
    )
    return 0


def _cmd_creator_add(args: argparse.Namespace) -> int:
    with _creators_repo() as repo:
        try:
            identity = creators.add_creator(
                args.reference,
                content_source=YouTubeContentSource(),
                repo=repo,
                # Seed the recent back-catalogue as metadata-only Candidates
                # (issue #7); the window is tunable via BACKFILL_CUTOFF_DAYS.
                cutoff=config.backfill_cutoff(),
            )
        except CreatorResolutionError as exc:
            logger.error("%s", exc)
            return 1
    logger.info("added Creator %s (%s)", identity.name, identity.channel_id)
    return 0


def _cmd_creator_remove(args: argparse.Namespace) -> int:
    with _creators_repo() as repo:
        removed = creators.remove_creator(args.channel_id, repo=repo)
    if removed:
        logger.info("removed Creator %s", args.channel_id)
        return 0
    logger.warning("no Creator with channel_id %s", args.channel_id)
    return 1


def _cmd_creator_list(_args: argparse.Namespace) -> int:
    with _creators_repo() as repo:
        watched = repo.list_creators()
    for creator in watched:
        print(f"{creator.channel_id}\t{creator.name}")
    if not watched:
        logger.info("no Creators on the watch list yet")
    return 0


class _creators_repo:
    """Open a schema-ready Repository for the creator commands and close it.

    Uses only DATABASE_URL, so editing the watch list needs no other secrets.
    """

    def __enter__(self) -> Repository:
        self._conn = connect(config.database_url())
        init_schema(self._conn)
        return Repository(self._conn)

    def __exit__(self, *exc_info) -> None:
        self._conn.close()


if __name__ == "__main__":
    sys.exit(main())
