"""Environment-variable configuration.

API keys (OpenAI, Claude, Resend) are read from the environment and never
hard-coded (PRD #1 acceptance criterion; user story 30). Construction is lazy:
importing this module touches no environment, so tests can import the package
without any secrets present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

# Which LLM provider backs the chat tasks — summarize, extract, answer, judge,
# propose Concepts (ADR-0012). Defaults to OpenAI so the whole system runs on one
# external LLM provider (embeddings and Whisper already use OPENAI_API_KEY);
# `LLM_PROVIDER=anthropic` swaps in Claude instead. Selection lives in
# `health_bok.llm`.
DEFAULT_LLM_PROVIDER = "openai"

# The per-provider default chat model, used by every task that doesn't pin its own
# (SUMMARY_MODEL / EXTRACTION_MODEL / QUERY_MODEL / STANCE_MODEL /
# CONCEPT_PROPOSAL_MODEL). The OpenAI default is the strongest general model; tune
# down per-task for cost if wanted.
DEFAULT_OPENAI_MODEL = "gpt-4.1"
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

# Part 2 (issue #13). The chat tasks default to the provider's default chat model
# (see `default_chat_model`) and are each tunable separately. The embedding model
# is pinned by ADR-0008 and stays on OpenAI.
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


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


def _float(name: str, default: float) -> float:
    """Read an optional positive-float knob, falling back to `default`."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name!r} must be a number") from None
    if value <= 0:
        raise ConfigError(f"Environment variable {name!r} must be positive")
    return value


def _bool(name: str, default: bool) -> bool:
    """Read an optional boolean knob ('1'/'true'/'yes' are true; blank → default)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def llm_provider() -> str:
    """Which provider backs the chat tasks — `openai` (default) or `anthropic`.

    Normalized to lower-case; the value is validated where it is acted on
    (`health_bok.llm.chat_model`), which raises ConfigError on anything else.
    """
    return os.environ.get("LLM_PROVIDER", DEFAULT_LLM_PROVIDER).strip().lower()


def default_chat_model() -> str:
    """The default chat model for the configured provider (ADR-0012)."""
    return DEFAULT_CLAUDE_MODEL if llm_provider() == "anthropic" else DEFAULT_OPENAI_MODEL


def anthropic_api_key() -> str:
    """The Claude API key — required only when `LLM_PROVIDER=anthropic` (ADR-0012)."""
    return _require("ANTHROPIC_API_KEY")


def openai_api_key() -> str:
    """The OpenAI API key — embeddings, Whisper, and (by default) the chat tasks."""
    return _require("OPENAI_API_KEY")


def extraction_model() -> str:
    """The model used for Claim/Protocol extraction (ADR-0010, ADR-0012).

    Defaults to the configured provider's default model; pin EXTRACTION_MODEL to
    use a different one (e.g. a stronger model for precision-first extraction).
    """
    return os.environ.get("EXTRACTION_MODEL") or default_chat_model()


def embedding_model() -> str:
    """The embedding model used for Concept normalization (ADR-0008)."""
    return os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)


def concept_merge_distance() -> float:
    """Cosine-distance threshold below which two mentions are the same Concept."""
    from .concepts import DEFAULT_MERGE_DISTANCE

    return _float("CONCEPT_MERGE_DISTANCE", DEFAULT_MERGE_DISTANCE)


def summary_model() -> str:
    """The model used to summarize a Transcript for the Digest (ADR-0012).

    Defaults to the configured provider's default chat model; tunable via
    SUMMARY_MODEL (e.g. a cheaper model for bulk summarization). Replaces the old
    provider-named CLAUDE_MODEL knob.
    """
    return os.environ.get("SUMMARY_MODEL") or default_chat_model()


def query_model() -> str:
    """The model used to synthesize grounded query answers (ADR-0011, ADR-0012).

    Defaults to the configured provider's default chat model; tunable via
    QUERY_MODEL if grounded Q&A warrants a different one from extraction.
    """
    return os.environ.get("QUERY_MODEL") or default_chat_model()


def query_concept_limit() -> int:
    """How many nearest Concepts a question retrieves through (issue #17)."""
    from .query import DEFAULT_CONCEPT_LIMIT

    return _positive_int("QUERY_CONCEPT_LIMIT", DEFAULT_CONCEPT_LIMIT)


def query_max_distance() -> float:
    """Cosine-distance cutoff beyond which a Concept does not cover a question.

    The query honesty knob (ADR-0011): a question far from everything in the
    library retrieves no Concept and the answer abstains. Tunable via
    QUERY_MAX_DISTANCE.
    """
    from .query import DEFAULT_MAX_DISTANCE

    return _float("QUERY_MAX_DISTANCE", DEFAULT_MAX_DISTANCE)


def query_evidence_limit() -> int:
    """Per-category cap on evidence handed to the answerer (issue #17)."""
    from .query import DEFAULT_EVIDENCE_LIMIT

    return _positive_int("QUERY_EVIDENCE_LIMIT", DEFAULT_EVIDENCE_LIMIT)


def stance_model() -> str:
    """The model the StanceJudge uses for change detection (issue #18, ADR-0012).

    Defaults to the configured provider's default chat model; tunable via
    STANCE_MODEL if judging stances warrants a different one.
    """
    return os.environ.get("STANCE_MODEL") or default_chat_model()


def concept_proposal_model() -> str:
    """The model the ConceptProposer uses to propose new Concepts (issue #39, ADR-0012).

    Defaults to the configured provider's default chat model; tunable via
    CONCEPT_PROPOSAL_MODEL if proposing Concept terms warrants a different one.
    """
    return os.environ.get("CONCEPT_PROPOSAL_MODEL") or default_chat_model()


def hierarchy_proposal_model() -> str:
    """The model the HierarchyProposer uses to propose broader-of parents (ADR-0013).

    Defaults to the configured provider's default chat model; tunable via
    HIERARCHY_PROPOSAL_MODEL if proposing taxonomy parents warrants a different one.
    """
    return os.environ.get("HIERARCHY_PROPOSAL_MODEL") or default_chat_model()


def impact_candidate_limit() -> int:
    """Per-category cap on candidates one Impact detection pass judges (issue #18).

    The ceiling on LLM judge calls a single detection makes; tunable via
    IMPACT_CANDIDATE_LIMIT.
    """
    from .impacts import DEFAULT_CANDIDATE_LIMIT

    return _positive_int("IMPACT_CANDIDATE_LIMIT", DEFAULT_CANDIDATE_LIMIT)


def webapp_base_url() -> str:
    """Public base URL of the Web App, for Digest deep-links (ADR-0007).

    Optional: when unset the Digest still lists items, just without the "review in
    the Web App" deep-link. Trailing slash is trimmed so links compose cleanly.
    """
    return os.environ.get("WEBAPP_BASE_URL", "").rstrip("/")


def digest_enabled() -> bool:
    """Whether the daily Digest email is sent at all (ADR-0007).

    Email is a notification, never essential — the system stays fully usable with
    it off (set DIGEST_ENABLED=false), in which case the Resend secrets are not
    required and no Digest is sent.
    """
    return _bool("DIGEST_ENABLED", True)


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
    """Runtime configuration assembled from environment variables.

    Shared by the daily pipeline, the worker, and the API. The Resend fields are
    only required when the Digest is enabled (ADR-0007): with email off the
    pipeline runs without those secrets present.
    """

    database_url: str
    openai_api_key: str
    summary_model: str
    summary_max_chars: int
    summary_chunk_chars: int
    # Part 2 (issue #13).
    extraction_model: str
    embedding_model: str
    concept_merge_distance: float
    webapp_base_url: str
    digest_enabled: bool
    # Resend — present only when the Digest is enabled.
    resend_api_key: str
    digest_from: str
    digest_recipient: str

    @classmethod
    def from_env(cls) -> "Config":
        """Build configuration from the process environment.

        Raises ConfigError if any required secret or address is missing, so a
        misconfigured deploy fails loudly at startup rather than mid-run. The
        Resend secrets are only required when DIGEST_ENABLED is on.
        """
        enabled = digest_enabled()
        return cls(
            database_url=_require("DATABASE_URL"),
            # Powers the chat tasks by default (ADR-0012), the Whisper transcription
            # fallback for caption-less videos (PRD #1, user story 10), and
            # Concept-embedding (ADR-0008). The chat-provider key — this one for
            # OpenAI, or ANTHROPIC_API_KEY when LLM_PROVIDER=anthropic — is required
            # lazily by the `health_bok.llm` factory, not here.
            openai_api_key=_require("OPENAI_API_KEY"),
            summary_model=summary_model(),
            # The map-reduce threshold and chunk size (issue #6) are tunable but
            # optional — sensible defaults keep a fresh deploy summarizing.
            summary_max_chars=_positive_int(
                "SUMMARY_MAX_CHARS", DEFAULT_SUMMARY_MAX_CHARS
            ),
            summary_chunk_chars=_positive_int(
                "SUMMARY_CHUNK_CHARS", DEFAULT_SUMMARY_CHUNK_CHARS
            ),
            extraction_model=extraction_model(),
            embedding_model=embedding_model(),
            concept_merge_distance=concept_merge_distance(),
            webapp_base_url=webapp_base_url(),
            digest_enabled=enabled,
            resend_api_key=_require("RESEND_API_KEY") if enabled else "",
            digest_from=_require("DIGEST_FROM") if enabled else "",
            digest_recipient=_require("DIGEST_RECIPIENT") if enabled else "",
        )
