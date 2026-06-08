# Single self-hosted Postgres as the source of truth

All data — raw transcripts, provenance, the Status time-series, and the graph (nodes, an edges
table, and `pgvector` embeddings) — lives in one self-hosted Postgres instance on the VPS.

## Considered Options

- **SQLite-only** — rejected: weaker path to a likely Supabase/cloud future, and `sqlite-vec`
  is less mature than `pgvector` for the change-detection feature.
- **Neo4j as system of record**, and the **SQLite + Neo4j hybrid** — rejected: two systems of
  record means dual-write consistency problems and cross-store references with no transactional
  integrity, buying graph ergonomics we don't need at personal scale (low thousands of nodes,
  where recursive CTEs traverse fine).

Postgres collapses relational + time-series + recursive-CTE graph traversal + vector search
into one engine. If interactive graph *visualization* is ever wanted, Neo4j is permitted only
as a re-derivable read **projection**, never a source of truth. Cloud migration to Supabase is
kept a `pg_dump` away.
