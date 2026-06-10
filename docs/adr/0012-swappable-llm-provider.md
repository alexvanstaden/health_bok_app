# LLM provider is swappable behind one ChatModel seam, defaulting to OpenAI

Four tasks call a chat LLM: summarization (ADR-0007), precision-first extraction (ADR-0010),
grounded query synthesis (ADR-0011), and the Impact StanceJudge (issue #18). Each adapter
constructed its own Anthropic client and called the Messages API directly, so the provider was
welded into four places. We want to run on **one** external LLM provider — OpenAI already does
Whisper transcription and Concept embeddings (ADR-0008) — without losing the ability to swap.

## Decision

Introduce a single provider-neutral port, **`ChatModel`**, with one method —
`complete(system, user, max_tokens) -> str`. It is the only LLM-transport seam.

- **Two transport adapters** implement it: `OpenAIChatModel` (Chat Completions) and
  `AnthropicChatModel` (Messages). They are tiny — build the request, unwrap the text.
- **The four feature adapters keep their prompts and parsing** and depend only on an injected
  `ChatModel`. The provider-specific code is *only* the two transports; prompt-building and the
  JSON contracts (`parse_extraction`, `parse_stance`, `parse_answer`) stay provider-agnostic and
  unit-tested without a network.
- **One factory** (`health_bok/llm.py`) reads `LLM_PROVIDER` and constructs the matching
  transport with the right key. Selecting a provider touches this factory, nothing else.
- **OpenAI is the default.** The system then needs one external LLM provider, not two; the
  daily pipeline, worker, and API already require `OPENAI_API_KEY`. `LLM_PROVIDER=anthropic`
  swaps Claude back in, and only then is `ANTHROPIC_API_KEY` required.
- **Per-task model knobs stay** (`SUMMARY_MODEL`, `EXTRACTION_MODEL`, `QUERY_MODEL`,
  `STANCE_MODEL`), each defaulting to the provider's default chat model (OpenAI → `gpt-4.1`), so
  a task can use a stronger or cheaper model without changing provider. The provider-named
  `CLAUDE_MODEL` knob is renamed `SUMMARY_MODEL`.

## Considered Options

- **Per-port provider selection** (each task on a different provider) — rejected for now: it
  multiplies config and required keys, working against the "one provider" goal. The seam already
  allows it later (the factory takes the model id), if a task ever warrants a specific provider.
- **Drop Anthropic entirely** — rejected: keeping the second transport is a few lines and
  preserves the swappability that is half the point; a provider outage or a model regression is
  then a one-env-var fallback, not a code change.
- **A heavier provider abstraction** (streaming, tool-use, token accounting) — rejected as
  premature: all four tasks are single-turn prompt→text(→JSON). The minimal `complete` seam is
  enough; widen it only when a task needs more.

## Consequences

- Embeddings and Whisper are unchanged and remain OpenAI-only (ADR-0008) — they are not chat
  tasks and sit outside this seam.
- JSON reliability now rests on the prompts ("respond with ONLY a JSON object") plus the
  fence-tolerant parsers, across providers. The parsers already default-or-drop on malformed
  output, so a provider that wraps or chatters degrades safely rather than crashing.
- Tests inject a fake `ChatModel`, so feature-adapter behaviour is exercised with no SDK and no
  network; the two transports are unit-tested with their SDK clients faked.
