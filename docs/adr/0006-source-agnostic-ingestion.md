# Ingestion is source-type-agnostic; YouTube is just the first adapter

Content enters from many source types — YouTube videos, web articles, X posts, and the owner's
own pasted LLM-research output — and all of them flow through the **same** pipeline to become
Body-of-Knowledge content:

    acquire raw content + provenance → archive (ADR-0001) → extract → Candidate → approval
    (ADR-0004) → Body of Knowledge

Only the first step (acquisition) is type-specific, handled by a thin per-type adapter;
everything downstream is identical. The owner's separately-conducted research is **not**
special-cased — it is just another Source carrying a link to its origin.

## Consequences

- **Provenance granularity varies by type**, and the model must accommodate it: a Claim cites a
  **Source plus an *optional* locator** — a timestamp deep-link for video, a quoted span/anchor
  for an article, or nothing finer than the URL for a short post or a research paste. The
  locator must be allowed to be absent.
- **v1 scope is unaffected:** automated *discovery* stays YouTube-only (RSS polling of Creators).
  Other source types enter by **manual submission** (paste content + link), through the same
  processing pipeline — a different front door, not a different pipeline.
