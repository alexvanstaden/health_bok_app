"""The provider-neutral ChatModel seam (ADR-0012) — pure, no network.

Every chat-backed adapter (Summarizer, Extractor, QueryAnswerer, StanceJudge,
ConceptProposer) talks to its provider only through `ChatModel.complete(...)`. Two
transport adapters implement it — OpenAI and Anthropic — and a factory picks one
from `LLM_PROVIDER`. These guard the transport shape (how each SDK is called and
its response unwrapped) and the provider selection, with both SDK clients faked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from health_bok.adapters.anthropic_chat import AnthropicChatModel
from health_bok.adapters.openai_chat import OpenAIChatModel


def test_openai_chat_sends_system_and_user_and_unwraps_content(monkeypatch):
    seen = {}

    class _FakeOpenAI:
        def __init__(self, api_key):
            seen["api_key"] = api_key
            self.chat = SimpleNamespace(completions=self)

        def create(self, *, model, max_tokens, messages):
            seen["model"] = model
            seen["max_tokens"] = max_tokens
            seen["messages"] = messages
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="  hi there  "))]
            )

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)

    model = OpenAIChatModel("sk-test", "gpt-4.1")
    out = model.complete(system="SYS", user="USR", max_tokens=64)

    assert out == "hi there"  # trimmed
    assert seen["model"] == "gpt-4.1" and seen["max_tokens"] == 64
    assert seen["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]


def test_anthropic_chat_passes_system_apart_and_joins_text_blocks(monkeypatch):
    seen = {}

    class _FakeAnthropic:
        def __init__(self, api_key):
            self.messages = self

        def create(self, *, model, max_tokens, system, messages):
            seen["system"] = system
            seen["messages"] = messages
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="part one "),
                    SimpleNamespace(type="thinking", text="ignored"),
                    SimpleNamespace(type="text", text="part two"),
                ]
            )

    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)

    model = AnthropicChatModel("sk-ant", "claude-sonnet-4-6")
    out = model.complete(system="SYS", user="USR", max_tokens=64)

    assert out == "part one part two"  # only text blocks, joined and trimmed
    assert seen["system"] == "SYS"  # system is a top-level arg, not a message
    assert seen["messages"] == [{"role": "user", "content": "USR"}]


def test_factory_defaults_to_openai(monkeypatch):
    from health_bok import llm

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    model = llm.chat_model("gpt-4.1")
    assert isinstance(model, OpenAIChatModel)


def test_factory_selects_anthropic_when_configured(monkeypatch):
    from health_bok import llm

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")

    model = llm.chat_model("claude-sonnet-4-6")
    assert isinstance(model, AnthropicChatModel)


def test_factory_rejects_unknown_provider(monkeypatch):
    from health_bok import config, llm

    monkeypatch.setenv("LLM_PROVIDER", "groq")
    with pytest.raises(config.ConfigError):
        llm.chat_model("whatever")


class _FakeChat:
    """A `ChatModel` that records its prompt and returns a canned reply."""

    def __init__(self, reply: str):
        self.reply = reply
        self.seen: dict = {}

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        self.seen = {"system": system, "user": user, "max_tokens": max_tokens}
        return self.reply


def test_extractor_feeds_prompt_to_chat_and_parses_reply():
    # A feature adapter builds the prompt + parses; the ChatModel is the only seam.
    from health_bok.adapters.extractor import ChatExtractor
    from health_bok.models import FetchedTranscript, Provenance, TranscriptSegment

    chat = _FakeChat(
        '{"claims": [{"text": "Zone 2 builds mitochondria.", '
        '"locator_seconds": 90, "type": "mechanism", "concepts": ["zone 2"]}], '
        '"protocols": []}'
    )
    transcript = FetchedTranscript(
        provenance=Provenance(
            video_id="v1", title="Cardio", channel_id="c1", channel_name="Coach",
            published_at=None,
        ),
        segments=[TranscriptSegment(text="Zone 2 builds mitochondria.", start=90.0, duration=4.0)],
    )

    extraction = ChatExtractor(chat).extract(transcript)

    assert [c.text for c in extraction.claims] == ["Zone 2 builds mitochondria."]
    assert "[90s] Zone 2 builds mitochondria." in chat.seen["user"]  # timestamped prompt
    assert chat.seen["system"]  # the precision-first system prompt was passed through
