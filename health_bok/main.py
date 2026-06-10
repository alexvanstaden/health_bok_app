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

Creator management needs only ``DATABASE_URL`` plus the YouTube adapter, so it
never requires the Digest or LLM secrets to be set.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import config, creators, llm
from .adapters.embedder import OpenAIEmbedder
from .adapters.extractor import ChatExtractor
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
    Embedder (OpenAI) behind their ports and runs the worker. Polls forever by
    default — the docker `worker` service — or drains once with ``--once`` for an
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
            )
            if handled:
                logger.info("worker drained %d job(s)", handled)
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        conn.close()
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
