# Physical knowledge-graph schema: typed entity tables + one polymorphic edges table + a vector table

ADR-0003 chose Postgres for "the graph (nodes, an edges table, and pgvector embeddings)"
but never said what "nodes" physically meant. This pins it.

## Decision

- **Typed entity tables**, one per first-class entity (`claims`, `protocols`, `concepts`,
  `decisions`, `goals`, `markers`, and `impacts`). Each carries its own columns, CHECKs, and
  real FKs — so a Protocol's `dose/timing/frequency`, a Marker reading's `value/unit/range`,
  and a Claim's provenance/locator are typed and queryable, not buried in JSONB.
- **1:many *ownership* stays as FK columns** (e.g. `claims.source_id`, a marker reading's
  `concept_id`) — not edges.
- **One polymorphic `edges` table** holds every genuinely graph-shaped relationship — the
  many:many, computed, or otherwise traversable ones (`claim → concept references`,
  `claim → protocol supports`, `decision → protocol implements`, `decision → goal serves`,
  `decision → marker motivated_by`):
  `edges(id, src_type, src_id, dst_type, dst_id, kind, props jsonb, created_at)`.
  - `unique (src_type, src_id, dst_type, dst_id, kind)` makes edge-writes **idempotent**, so
    re-extraction (ADR-0005) re-asserts edges without dup-checking.
  - Polymorphic `src/dst` can't be real FKs; **referential integrity is enforced by a
    lightweight insert/update trigger** (same pattern Part 1 uses for transcript immutability),
    fail-loud, so no dangling edges accumulate.
  - `kind` is constrained by a CHECK / small lookup, not a Postgres `ENUM` (enums are painful
    to alter as edge kinds grow).
- **Impact is a typed entity, not an edge** — it carries a stance and a lifecycle
  (`new → reviewed → actioned | dismissed`) and *points at* an anchor via a polymorphic ref.
- **One append-only `embeddings` table** (pgvector):
  `embeddings(id, owner_type, owner_id, embedding vector(1536), model, created_at)`,
  HNSW-indexed. Claims, Concepts, and Protocols are embedded; **Transcripts are not** — search
  is over the extracted layer, not raw text. Model: OpenAI `text-embedding-3-small` (the
  `OPENAI_API_KEY` secret already exists; Supabase-portable). Append-only + model-stamped, so
  re-embedding against a better model is clean — same spirit as ADR-0001.

## Considered Options

- **Generic property graph** (one `nodes` table + one `edges` table, all JSONB props) —
  rejected: discards the relational integrity and queryability ADR-0003 chose Postgres *for*;
  every entity becomes an untyped blob.
- **Per-relationship junction tables** (`claim_supports_protocol`, `claim_references_concept`,
  …) — rejected: graph traversal and the visualization projection must UNION across N tables,
  and every new edge kind is a migration. The integrity win is cheaper to get with FK-typed
  columns inside one edges table at personal scale.

## Consequences

- Recursive-CTE traversal (ADR-0003) and the optional visualization projection read **one**
  uniform edges source plus the typed node tables.
- At ADR-0003's "low thousands of nodes" scale, a single edges table traverses fine.
- Search and Impact candidate-generation run over embeddings of the extracted layer (Claims,
  Concepts, Protocols), never raw Transcripts.
