"""Entrypoint: the `health-bok` command-line interface.

Two responsibilities sit behind this CLI:

  * ``health-bok run`` (the default) wires the real adapters to the orchestrator
    and runs the daily pipeline: for every watched Creator it detects new uploads
    via RSS, summarizes them, and sends one Digest.
  * ``health-bok creators add|remove|list`` maintains the watch list of
    Creators, resolving an @handle/URL to a stable channel_id once at add-time.

Creator management needs only ``DATABASE_URL`` plus the YouTube adapter, so it
never requires the Digest or Summarizer secrets to be set.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import config, creators
from .adapters.claude import ClaudeSummarizer
from .adapters.resend import ResendDigestSender
from .adapters.whisper import WhisperTranscriber
from .adapters.youtube import YouTubeContentSource
from .config import Config
from .db import connect, init_schema
from .job import run_job
from .models import CreatorResolutionError
from .repository import Repository
from .summarizer import MapReduceSummarizer

logger = logging.getLogger("health_bok")


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
        result = run_job(
            content_source=YouTubeContentSource(),
            transcriber=WhisperTranscriber(cfg.openai_api_key),
            # Long Transcripts are map-reduced; short ones summarize in one pass.
            summarizer=MapReduceSummarizer(
                ClaudeSummarizer(cfg.anthropic_api_key, cfg.claude_model),
                max_chars=cfg.summary_max_chars,
                chunk_chars=cfg.summary_chunk_chars,
            ),
            digest_sender=ResendDigestSender(
                cfg.resend_api_key, cfg.digest_from, cfg.digest_recipient
            ),
            repo=repo,
            model=cfg.claude_model,
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
