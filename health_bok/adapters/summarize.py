"""Summarizer adapter over the ChatModel seam (ADR-0012).

Single-pass summarization of one Transcript into prose — the thin path. Long
Transcripts are handled by `MapReduceSummarizer` (health_bok.summarizer), which
wraps this adapter and calls it once per section and once to reduce, so this
adapter stays single-pass and length-agnostic. It builds the prompt and delegates
the provider call to an injected `ChatModel`, so it is provider-neutral: the model
(default the configured provider's, via SUMMARY_MODEL) is chosen by the factory.
"""

from __future__ import annotations

from ..models import FetchedTranscript
from ..ports import ChatModel

_SYSTEM = (
    "You summarize health & longevity videos for a daily digest. Write a concise, "
    "faithful prose summary of what the video covers — the key claims, protocols, "
    "and takeaways — in a few short paragraphs. Do not invent anything that is not "
    "in the transcript. Output prose only, no preamble."
)

_MAX_TOKENS = 1024


class ChatSummarizer:
    """Turns a Transcript into a prose Summary via an injected `ChatModel`."""

    def __init__(self, chat: ChatModel):
        self._chat = chat

    def summarize(self, transcript: FetchedTranscript) -> str:
        prov = transcript.provenance
        return self._chat.complete(
            system=_SYSTEM,
            user=(
                f"Video: {prov.title}\n"
                f"Channel: {prov.channel_name}\n\n"
                f"Transcript:\n{transcript.text}"
            ),
            max_tokens=_MAX_TOKENS,
        )
