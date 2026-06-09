"use client";

// Browse the Protocols of the Body of Knowledge (issue #14): structured,
// parameterized recommendations drawn from sources. Filterable by action text and
// (when arrived at from a Concept) by referenced Concept. Each row links to the
// Protocol's detail, where the owner sees its justifying Claims and edits/deletes
// it in place (ADR-0010).

import { useCallback, useEffect, useMemo, useState } from "react";
import { BokProtocol, listProtocols } from "../lib/api";

function paramLine(p: BokProtocol): string {
  return [
    p.dose && `dose: ${p.dose}`,
    p.timing && `timing: ${p.timing}`,
    p.frequency && `frequency: ${p.frequency}`,
    p.duration && `duration: ${p.duration}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

export default function ProtocolsList({
  searchParams,
}: {
  searchParams: { concept_id?: string };
}) {
  const conceptId = searchParams.concept_id ? Number(searchParams.concept_id) : undefined;
  const [protocols, setProtocols] = useState<BokProtocol[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [search, setSearch] = useState("");

  const refresh = useCallback(async () => {
    try {
      const { protocols } = await listProtocols({ conceptId });
      setProtocols(protocols);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [conceptId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const shown = useMemo(
    () => protocols.filter((p) => p.action.toLowerCase().includes(search.toLowerCase())),
    [protocols, search],
  );

  return (
    <>
      <h1>Protocols</h1>
      <p className="subtitle">
        Structured recommendations drawn from sources — {shown.length} shown.
      </p>
      {conceptId && (
        <p className="crumb">
          Filtered to one Concept. <a href="/protocols">Clear ✕</a>
        </p>
      )}
      <div className="filters">
        <input
          className="search"
          placeholder="Filter by action…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {error && <p className="error">API error: {error}</p>}
      {loaded && shown.length === 0 && !error && <p className="muted">No Protocols match.</p>}

      {shown.map((p) => (
        <a key={p.id} href={`/protocols/${p.id}`} className="list-item">
          <div className="row">
            <strong>{p.action}</strong>
            {p.protected && <span className="badge protected">edited</span>}
          </div>
          <div className="meta">
            {paramLine(p)}
            {p.concepts.length > 0 && " · " + p.concepts.map((c) => c.name).join(", ")}
          </div>
        </a>
      ))}
    </>
  );
}
