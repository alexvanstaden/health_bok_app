"""Entrypoint: run the slice-1 walking skeleton for real.

Wires the real adapters to the orchestrator, against the configured Postgres,
for the one configured video. Invoked via the `health-bok` console script or
`python -m health_bok`.
"""

from __future__ import annotations

import logging

from .adapters.claude import ClaudeSummarizer
from .adapters.resend import ResendDigestSender
from .adapters.youtube import YouTubeContentSource
from .config import Config
from .db import connect, init_schema
from .job import run_job
from .repository import Repository

logger = logging.getLogger("health_bok")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    config = Config.from_env()

    conn = connect(config.database_url)
    try:
        init_schema(conn)
        repo = Repository(conn)

        result = run_job(
            config.video_id,
            content_source=YouTubeContentSource(),
            summarizer=ClaudeSummarizer(config.anthropic_api_key, config.claude_model),
            digest_sender=ResendDigestSender(
                config.resend_api_key, config.digest_from, config.digest_recipient
            ),
            repo=repo,
            model=config.claude_model,
        )
    finally:
        conn.close()

    logger.info(
        "run complete: newly_processed=%s digest_sent=%s items=%s",
        result.newly_processed,
        result.digest_sent,
        result.digest_item_count,
    )


if __name__ == "__main__":
    main()
