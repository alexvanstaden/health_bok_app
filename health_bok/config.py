"""Environment-variable configuration.

API keys (Claude, Resend, OpenAI) are read from the environment and never
hard-coded (PRD #1 acceptance criterion; user story 30). Construction is lazy:
importing this module touches no environment, so tests can import the package
without any secrets present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

# Default summarization model: cost-effective bulk summarization, overridable to
# a higher-quality model via CLAUDE_MODEL (PRD #1, user story 17).
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

# Transcripts whose text exceeds this many characters are summarized via
# map-reduce instead of a single pass (issue #6); shorter ones stay single-pass.
# ~48k chars is roughly a long-form hour of speech — multi-hour podcasts cross it.
DEFAULT_SUMMARY_MAX_CHARS = 48_000
# Target size of each section when a long Transcript is chunked for map-reduce.
DEFAULT_SUMMARY_CHUNK_CHARS = 16_000

# How far back to seed a Creator's back-catalogue as Candidates when it is added
# (issue #7): ~2 years. Older uploads are skipped so the approval queue isn't
# flooded with a creator's ancient catalogue. Tunable via BACKFILL_CUTOFF_DAYS.
DEFAULT_BACKFILL_CUTOFF_DAYS = 730


class ConfigError(RuntimeError):
    """A required environment variable is missing."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"Required environment variable {name!r} is not set. "
            f"See .env.example."
        )
    return value


def _positive_int(name: str, default: int) -> int:
    """Read an optional positive-integer tuning knob, falling back to `default`.

    A blank value uses the default; a non-numeric or non-positive one fails
    loudly at startup rather than silently mis-tuning the pipeline mid-run.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name!r} must be an integer") from None
    if value <= 0:
        raise ConfigError(f"Environment variable {name!r} must be a positive integer")
    return value


def database_url() -> str:
    """The single source-of-truth Postgres URL (ADR-0003).

    Creator-management commands need only this, not the email/LLM secrets, so
    editing the watch list never requires the Digest or Summarizer keys to be
    present.
    """
    return _require("DATABASE_URL")


def backfill_cutoff() -> timedelta:
    """The backfill recency window applied when a Creator is added (issue #7).

    Optional and defaulted like the summarization knobs, so adding a Creator
    still needs only DATABASE_URL. Tunable via BACKFILL_CUTOFF_DAYS (a positive
    count of days); a bad value fails loudly at startup rather than silently
    backfilling the wrong window.
    """
    return timedelta(
        days=_positive_int("BACKFILL_CUTOFF_DAYS", DEFAULT_BACKFILL_CUTOFF_DAYS)
    )


@dataclass(frozen=True)
class Config:
    """Runtime configuration assembled from environment variables."""

    database_url: str
    anthropic_api_key: str
    resend_api_key: str
    openai_api_key: str
    claude_model: str
    summary_max_chars: int
    summary_chunk_chars: int
    digest_from: str
    digest_recipient: str

    @classmethod
    def from_env(cls) -> "Config":
        """Build configuration from the process environment.

        Raises ConfigError if any required secret or address is missing, so a
        misconfigured deploy fails loudly at startup rather than mid-run.
        """
        return cls(
            database_url=_require("DATABASE_URL"),
            anthropic_api_key=_require("ANTHROPIC_API_KEY"),
            resend_api_key=_require("RESEND_API_KEY"),
            # Powers the Whisper transcription fallback for caption-less videos
            # (PRD #1, user story 10).
            openai_api_key=_require("OPENAI_API_KEY"),
            claude_model=os.environ.get("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
            # The map-reduce threshold and chunk size (issue #6) are tunable but
            # optional — sensible defaults keep a fresh deploy summarizing.
            summary_max_chars=_positive_int(
                "SUMMARY_MAX_CHARS", DEFAULT_SUMMARY_MAX_CHARS
            ),
            summary_chunk_chars=_positive_int(
                "SUMMARY_CHUNK_CHARS", DEFAULT_SUMMARY_CHUNK_CHARS
            ),
            digest_from=_require("DIGEST_FROM"),
            digest_recipient=_require("DIGEST_RECIPIENT"),
        )
