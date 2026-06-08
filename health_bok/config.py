"""Environment-variable configuration.

API keys (Claude, Resend, OpenAI) are read from the environment and never
hard-coded (PRD #1 acceptance criterion; user story 30). Construction is lazy:
importing this module touches no environment, so tests can import the package
without any secrets present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Default summarization model: cost-effective bulk summarization, overridable to
# a higher-quality model via CLAUDE_MODEL (PRD #1, user story 17).
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

# Slice-1 walking skeleton runs against one known video; a hardcoded default is
# fine, but it stays overridable so the entrypoint is not literally a constant.
DEFAULT_VIDEO_ID = "dQw4w9WgXcQ"


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


def database_url() -> str:
    """The single source-of-truth Postgres URL (ADR-0003).

    Creator-management commands need only this, not the email/LLM secrets, so
    editing the watch list never requires the Digest or Summarizer keys to be
    present.
    """
    return _require("DATABASE_URL")


@dataclass(frozen=True)
class Config:
    """Runtime configuration assembled from environment variables."""

    database_url: str
    anthropic_api_key: str
    resend_api_key: str
    openai_api_key: str
    claude_model: str
    digest_from: str
    digest_recipient: str
    video_id: str

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
            # Read now even though Whisper fallback lands in a later slice, so the
            # deploy surface for the three keys is established here (issue #2 AC).
            openai_api_key=_require("OPENAI_API_KEY"),
            claude_model=os.environ.get("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
            digest_from=_require("DIGEST_FROM"),
            digest_recipient=_require("DIGEST_RECIPIENT"),
            video_id=os.environ.get("WALKING_SKELETON_VIDEO_ID", DEFAULT_VIDEO_ID),
        )
