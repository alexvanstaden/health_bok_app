"use client";

// Browse the Concepts of the Body of Knowledge (issue #14): the normalized,
// deduplicated hubs that Claims and Protocols reference (CONTEXT.md "Concept").
// Filterable by name; each row links to the Concept's detail, the pivot point for
// relatedness-by-shared-Concept — everything that references it, without a visual
// graph (ADR-0009). Concepts are normalized, not hand-curated, so there is no
// edit/delete here.

import { useCallback, useEffect, useMemo, useState } from "react";
import { BokConcept, listConcepts } from "../lib/api";

export default function ConceptsList() {
  const [concepts, setConcepts] = useState<BokConcept[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [search, setSearch] = useState("");

  const refresh = useCallback(async () => {
    try {
      const { concepts } = await listConcepts({});
      setConcepts(concepts);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const shown = useMemo(
    () => concepts.filter((c) => c.name.toLowerCase().includes(search.toLowerCase())),
    [concepts, search],
  );

  return (
    <>
      <h1>Concepts</h1>
      <p className="subtitle">
        The normalized hubs Claims and Protocols reference — {shown.length} shown.
      </p>
      <div className="filters">
        <input
          className="search"
          placeholder="Filter by name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {error && <p className="error">API error: {error}</p>}
      {loaded && shown.length === 0 && !error && <p className="muted">No Concepts match.</p>}

      {shown.map((c) => (
        <a key={c.id} href={`/concepts/${c.id}`} className="list-item">
          <div className="row">
            <strong>{c.name}</strong>
            {c.kind && <span className="badge">{c.kind}</span>}
            <span className="spacer" />
            <span className="muted">
              {c.reference_count} ref{c.reference_count === 1 ? "" : "s"}
            </span>
          </div>
        </a>
      ))}
    </>
  );
}
