"""Provider selection for the ChatModel seam (ADR-0012).

One place decides which LLM provider backs every chat-powered adapter. The feature
adapters (Summarizer, Extractor, QueryAnswerer, StanceJudge, ConceptProposer)
depend only on the `ChatModel` port; this factory reads `LLM_PROVIDER` and
constructs the matching transport with the right key, so swapping providers — or
dropping one to cut an external dependency — never touches those adapters.
"""

from __future__ import annotations

from . import config
from .ports import ChatModel

OPENAI = "openai"
ANTHROPIC = "anthropic"


def chat_model(model: str) -> ChatModel:
    """Build the configured `ChatModel` for `model` (a provider-specific model id).

    Defaults to OpenAI (the daily pipeline, worker, and API already need
    `OPENAI_API_KEY` for embeddings and Whisper, so this keeps the system on one
    LLM provider). `LLM_PROVIDER=anthropic` swaps in Claude instead; only then is
    `ANTHROPIC_API_KEY` required.
    """
    provider = config.llm_provider()
    if provider == OPENAI:
        from .adapters.openai_chat import OpenAIChatModel

        return OpenAIChatModel(config.openai_api_key(), model)
    if provider == ANTHROPIC:
        from .adapters.anthropic_chat import AnthropicChatModel

        return AnthropicChatModel(config.anthropic_api_key(), model)
    raise config.ConfigError(
        f"Unknown LLM_PROVIDER {provider!r}; expected {OPENAI!r} or {ANTHROPIC!r}."
    )
