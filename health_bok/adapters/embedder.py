"""OpenAI adapter for the Embedder port (ADR-0008).

Embeds Concept mentions (and, later, Claims/Protocols) into 1536-d vectors with
`text-embedding-3-small` — the model ADR-0008 pins, reusing the existing
`OPENAI_API_KEY` and staying Supabase-portable. The dimension matches
`embeddings.embedding vector(1536)`. The SDK is imported lazily, so the package
imports without openai installed and the orchestrator only sees the `Embedder`
port.
"""

from __future__ import annotations

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class OpenAIEmbedder:
    """Embeds text into a 1536-d vector via the OpenAI embeddings API."""

    def __init__(self, api_key: str, model: str = DEFAULT_EMBEDDING_MODEL):
        import openai

        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def embed(self, text: str) -> list[float]:
        response = self._client.embeddings.create(model=self._model, input=text)
        return list(response.data[0].embedding)
