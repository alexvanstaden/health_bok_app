"""Claude adapter for the Summarizer port.

Single-pass summarization of one Transcript into prose — the thin path. Long
Transcripts are handled by `MapReduceSummarizer` (health_bok.summarizer), which
wraps this adapter and calls it once per section and once to reduce, so this
adapter stays single-pass and length-agnostic. The model is configurable
(default claude-sonnet-4-6) to trade cost against quality.
"""

from __future__ import annotations

from ..models import FetchedTranscript

_SYSTEM = (
    "You summarize health & longevity videos for a daily digest. Write a concise, "
    "faithful prose summary of what the video covers — the key claims, protocols, "
    "and takeaways — in a few short paragraphs. Do not invent anything that is not "
    "in the transcript. Output prose only, no preamble."
)

_MAX_TOKENS = 1024


class ClaudeSummarizer:
    """Turns a Transcript into a prose Summary via the Claude API."""

    def __init__(self, api_key: str, model: str):
        # Imported lazily so the package imports without the SDK installed.
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def summarize(self, transcript: FetchedTranscript) -> str:
        prov = transcript.provenance
        message = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Video: {prov.title}\n"
                        f"Channel: {prov.channel_name}\n\n"
                        f"Transcript:\n{transcript.text}"
                    ),
                }
            ],
        )
        return "".join(
            block.text for block in message.content if block.type == "text"
        ).strip()
