"use client";

// A Concept's detail (issue #14): everything that references it — the Claims and
// Protocols — each a navigable link. This is the pivot the owner follows to see
// what the Body of Knowledge says about a supplement, mechanism, or intervention,
// by traversing the inbound `references` edges rather than reading a graph
// (ADR-0008, ADR-0009).

import { useCallback, useEffect, useState } from "react";
import { BokConcept, getConcept } from "../../lib/api";

export default function ConceptDetail({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const [concept, setConcept] = useState<BokConcept | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setConcept(await getConcept(id));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <p>
        <a href="/concepts">← All Concepts</a>
      </p>
      {error && <p className="error">API error: {error}</p>}
      {!concept && !error && <p className="muted">Loading…</p>}

      {concept && (
        <>
          <div className="row">
            <h1>{concept.name}</h1>
            {concept.kind && <span className="badge">{concept.kind}</span>}
          </div>
          <p className="subtitle">
            Referenced by {concept.claims.length} Claim
            {concept.claims.length === 1 ? "" : "s"} and {concept.protocols.length} Protocol
            {concept.protocols.length === 1 ? "" : "s"}.{" "}
            <a href={`/claims?concept_id=${concept.id}`}>Browse its Claims →</a>
          </p>

          <div className="connections">
            <h3>Claims referencing it</h3>
            {concept.claims.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              concept.claims.map((c) => (
                <a key={c.id} href={`/claims/${c.id}`} className="link-pill">
                  {c.text}
                </a>
              ))
            )}

            <h3>Protocols referencing it</h3>
            {concept.protocols.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              concept.protocols.map((p) => (
                <a key={p.id} href={`/protocols/${p.id}`} className="link-pill">
                  {p.action}
                </a>
              ))
            )}
          </div>
        </>
      )}
    </>
  );
}
