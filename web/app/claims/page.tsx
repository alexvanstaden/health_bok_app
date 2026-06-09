"use client";

// Browse the Claims of the Body of Knowledge (issue #14). A filterable list —
// by sub-kind, and (when arrived at from a Concept) by referenced Concept — over
// the admitted evidence layer. Each row links to the Claim's detail, where the
// owner follows its connections and edits or deletes it in place (ADR-0010).

import { useCallback, useEffect, useState } from "react";
import { BokClaim, listClaims } from "../lib/api";

const TYPES = ["mechanism", "principle", "finding"];

export default function ClaimsList({
  searchParams,
}: {
  searchParams: { concept_id?: string; type?: string };
}) {
  const conceptId = searchParams.concept_id ? Number(searchParams.concept_id) : undefined;
  const [type, setType] = useState<string | undefined>(searchParams.type);
  const [claims, setClaims] = useState<BokClaim[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { claims } = await listClaims({ conceptId, type });
      setClaims(claims);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [conceptId, type]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <>
      <h1>Claims</h1>
      <p className="subtitle">
        The atomic assertions of the Body of Knowledge — {claims.length} shown.
      </p>

      {conceptId && (
        <p className="crumb">
          Filtered to one Concept. <a href="/claims">Clear ✕</a>
        </p>
      )}

      <div className="row filters">
        <button className={!type ? "active" : ""} onClick={() => setType(undefined)}>
          All
        </button>
        {TYPES.map((t) => (
          <button key={t} className={type === t ? "active" : ""} onClick={() => setType(t)}>
            {t}
          </button>
        ))}
      </div>

      {error && <p className="error">API error: {error}</p>}
      {loaded && claims.length === 0 && !error && <p className="muted">No Claims match.</p>}

      {claims.map((c) => (
        <a key={c.id} href={`/claims/${c.id}`} className="list-item">
          <div>{c.text}</div>
          <div className="meta row">
            <span className="badge">{c.type}</span>
            {c.protected && <span className="badge protected">edited</span>}
            <span>· {c.source.title}</span>
            {c.concepts.length > 0 && (
              <span>· {c.concepts.map((x) => x.name).join(", ")}</span>
            )}
          </div>
        </a>
      ))}
    </>
  );
}
