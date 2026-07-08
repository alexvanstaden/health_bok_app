"""OpenAI transport for the ChatModel port (ADR-0012).

Wraps the OpenAI Chat Completions API as a provider-neutral `ChatModel`: a single
system+user turn in, the response text out. Every feature adapter (Summarizer,
Extractor, QueryAnswerer, StanceJudge, ConceptProposer) builds its own prompts and
parses its own output against this seam, so the choice of provider lives in one
factory, not in five adapters. The SDK is imported lazily, so importing the package
needs no `openai` install. Reuses the same `OPENAI_API_KEY` the Embedder and
Whisper already use — the point of the switch is one fewer external provider.
"""

from __future__ import annotations

from ..ports import TruncatedCompletion


class OpenAIChatModel:
    """A `ChatModel` backed by the OpenAI Chat Completions API."""

    def __init__(self, api_key: str, model: str):
        import openai

        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        completion = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        choice = completion.choices[0]
        # A reply cut off at the budget would otherwise reach the caller's parser as
        # half a string; fail honestly here instead (ADR-0012).
        if choice.finish_reason == "length":
            raise TruncatedCompletion(
                max_tokens=max_tokens, provider_reason="finish_reason=length"
            )
        return (choice.message.content or "").strip()
