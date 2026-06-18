"use client";

// A Concept's neighbourhood (issue #51, ADR-0013): the lateral, Strength-ranked
// map of what it connects to. Each relationship is a directed, signed-predicate
// link (src → predicate → dst), ranked by evidence Strength, flagged when the pair
// is contested, and clickable through to the evidencing Claims — each carrying its
// Source + locator deep-link, the same Citations natural-language Query shows, so
// the two surfaces tell one consistent story. Query stays the primary way to
// *explore* the library (ADR-0009/0011); this is the visual map of connections.

import { useCallback, useEffect, useState } from "react";
import {
  Neighbourhood,
  NeighbourRelation,
  RelationCitation,
  getConceptNeighbourhood,
} from "../../../lib/api";

export default function ConceptNeighbourhood({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const [hood, setHood] = useState<Neighbourhood | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setHood(await getConceptNeighbourhood(id));
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
        <a href={`/concepts/${id}`}>← Back to Concept</a>
      </p>
      {error && <p className="error">API error: {error}</p>}
      {!hood && !error && <p className="muted">Loading…</p>}

      {hood && (
        <>
          <h1>{hood.concept.name} — Neighbourhood</h1>
          <p className="subtitle">
            What this Concept connects to, ranked by evidence Strength and drawn from
            your own Claims. Each connection links through to the Claims behind it.
            To <em>explore</em> in prose, use{" "}
            <a href="/ask">Ask</a>.
          </p>

          {hood.relations.length === 0 ? (
            <p className="muted">
              No relationships yet. A relationship appears only when your Claims
              connect this Concept to another one.
            </p>
          ) : (
            hood.relations.map((r) => <Relation key={r.relation_id} r={r} />)
          )}
        </>
      )}
    </>
  );
}

// Turn a stored predicate ("risk_factor_for") into a readable label
// ("risk factor for"); the predicate name itself carries the sign (ADR-0013).
function predicateLabel(predicate: string): string {
  return predicate.replace(/_/g, " ");
}

function Relation({ r }: { r: NeighbourRelation }) {
  return (
    <section className="card relation">
      <div className="link">
        <a href={`/concepts/${r.src.id}/neighbourhood`}>{r.src.name}</a>
        <span className="arrow">—</span>
        <span className="predicate">{predicateLabel(r.predicate)}</span>
        <span className="arrow">→</span>
        <a href={`/concepts/${r.dst.id}/neighbourhood`}>{r.dst.name}</a>
      </div>

      <div className="row meta" style={{ marginTop: "0.4rem" }}>
        <span className="badge">strength {r.strength.toFixed(2)}</span>
        <span className="badge">
          {r.creator_count} creator{r.creator_count === 1 ? "" : "s"}
        </span>
        {r.contested && <span className="badge contested">contested</span>}
        {r.via && (
          <span className="badge">via {r.via.name}</span>
        )}
      </div>

      <ol className="evidence">
        {r.evidence.map((c) => (
          <li key={c.claim_id}>
            <Cite c={c} />
          </li>
        ))}
      </ol>
    </section>
  );
}

// The same citation shape Ask renders: the Claim as a link, with its Source and
// locator deep-link on a muted line (ADR-0011).
function Cite({ c }: { c: RelationCitation }) {
  return (
    <span className="cite">
      <a href={`/claims/${c.claim_id}`}>{c.text}</a>
      <span className="meta row">
        <span className="badge">{c.type}</span>
        <span>· {c.source_title}</span>
        <span>
          ·{" "}
          <a href={c.deep_link} target="_blank" rel="noreferrer">
            watch source ↗
          </a>
        </span>
      </span>
    </span>
  );
}
