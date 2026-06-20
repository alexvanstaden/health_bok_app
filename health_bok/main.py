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
  * ``health-bok hierarchy propose|export|apply`` is the no-UI curation round-trip
    for the owner-curated `broader-of` taxonomy (issue #65): propose broader parents
    across the existing catalogue, export them to a CSV the owner edits, and apply the
    edited CSV (confirm/reject/repick). `propose` needs the LLM + DB secrets;
    `export`/`apply` need only the DB.

Creator management needs only ``DATABASE_URL`` plus the YouTube adapter, so it
never requires the Digest or LLM secrets to be set.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import config, creators, hierarchy_backfill, llm
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
        normalizer = ConceptNormalizer(
            OpenAIEmbedder(config.openai_api_key(), config.embedding_model()),
            repo,
            model=config.embedding_model(),
            merge_distance=config.concept_merge_distance(),
        )
        extractor = ChatExtractor(llm.chat_model(config.extraction_model()))
        # After admission, the forward Impact pass judges the new Claims/Protocols
        # against the owner's anchors (issue #18) — failure-isolated, so a judge
        # hiccup never undoes an admission.
        judge = ChatStanceJudge(llm.chat_model(config.stance_model()))
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
