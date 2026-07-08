"""Anthropic transport for the ChatModel port (ADR-0012).

Wraps the Claude Messages API as a provider-neutral `ChatModel`, the swappable
alternative to `OpenAIChatModel`: set `LLM_PROVIDER=anthropic` and the same feature
adapters run on Claude instead, unchanged. Anthropic takes the system prompt as a
top-level argument (not a message) and returns a list of content blocks, so this
joins the text blocks back into one string. The SDK is imported lazily, so
importing the package needs no `anthropic` install.
"""

from __future__ import annotations

from ..ports import TruncatedCompletion


class AnthropicChatModel:
    """A `ChatModel` backed by the Anthropic (Claude) Messages API."""

    def __init__(self, api_key: str, model: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # A reply cut off at the budget would otherwise reach the caller's parser as
        # half a string; fail honestly here instead (ADR-0012).
        if message.stop_reason == "max_tokens":
            raise TruncatedCompletion(
                max_tokens=max_tokens, provider_reason="stop_reason=max_tokens"
            )
        return "".join(b.text for b in message.content if b.type == "text").strip()
