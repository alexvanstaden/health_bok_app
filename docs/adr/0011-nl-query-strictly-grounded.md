# Natural-language query is strictly grounded in the owner's Body of Knowledge + personal layer

Graph visualization is out of v1 scope (ADR-0009); natural-language query is the primary way the
owner *explores* the Body of Knowledge. This fixes what that query is — and, crucially, what it
refuses to be.

## Decision

The Web App's natural-language query is a **grounded Q&A** assistant:

- **Strictly grounded.** It answers *only* from the owner's curated Body of Knowledge (Claims,
  Protocols) and **personal layer** (Goals, Markers, Decisions) — never the model's own general
  medical knowledge.
- **Always cited.** Every answer cites the specific Claims behind it, each clickable through to its
  Source and locator (timestamp deep-link for video). No ungrounded sentences.
- **Abstains honestly.** When the library does not cover a question, it says "nothing in your
  library covers this" rather than confabulating.
- **Personal scope is included** so answers are *actionable* — "does anything I've admitted
  contradict my magnesium Decision?", "what are my options for lowering apoB given my last reading?"
- **Retrieval reuses existing machinery** — hybrid pgvector + Concept traversal over the same
  `embeddings` (ADR-0008) the Impact engine uses; not a new subsystem.

## Considered Options

- **Hybrid (BoK + general LLM knowledge, blended)** — rejected: the system's whole value is the
  owner's *curated creators* (a generic answer is one chatbot prompt away), and blending
  unprovenanced general knowledge into a health tool is unsafe and quietly erodes trust in the
  citations. If general knowledge is ever wanted, it must be a clearly-labelled *separate* mode,
  never the default blend.
- **Plain semantic search (ranked list of hits, no synthesis)** — rejected as the smart layer: the
  owner wants answers, not just hits. (Filterable browse lists still exist as table stakes.)

## Consequences

- The query prompt is constrained to **cite-or-abstain**; "not in your library" is a first-class
  answer.
- Answer quality is bounded by Concept normalization and the precision-first extraction bar
  (ADR-0010) — query quality is BoK quality.
- The same retrieval path underpins both query and Impact candidate-generation, so improvements
  compound.
