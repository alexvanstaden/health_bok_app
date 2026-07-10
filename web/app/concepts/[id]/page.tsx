"use client";

// A Concept's detail (issue #14): everything that references it — the Claims and
// Protocols — each a navigable link. This is the pivot the owner follows to see
// what the Body of Knowledge says about a supplement, mechanism, or intervention,
// by traversing the inbound `references` edges rather than reading a graph
// (ADR-0008, ADR-0009).
//
// It also surfaces the `broader-of` hierarchy (issue #87, ADR-0013): the Concept's
// confirmed broader parents and any pending proposals, with a search-to-attach
// picker (seeded by the LLM suggester) and a remove control. A hand attach lands
// confirmed at once — hierarchy is the one link the owner curates — while a proposal
// waits for a one-click confirm; both remove through the same reject endpoint.

import { useCallback, useEffect, useState } from "react";
import {
  BokConcept,
  ConceptRef,
  confirmBroaderOf,
  getBroaderOfSuggestions,
  getConcept,
  listConcepts,
  rejectBroaderOf,
} from "../../lib/api";
import AttachParent from "../AttachParent";

export default function ConceptDetail({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const [concept, setConcept] = useState<BokConcept | null>(null);
  const [catalogue, setCatalogue] = useState<BokConcept[]>([]);
  const [seeds, setSeeds] = useState<ConceptRef[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [c, { concepts }] = await Promise.all([getConcept(id), listConcepts({})]);
      setConcept(c);
      setCatalogue(concepts);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  // Seed the attach picker with the LLM's suggested broader parents. Best-effort:
  // the suggester needs an embedding + model, so a failure just leaves no seeds.
  useEffect(() => {
    getBroaderOfSuggestions(id)
      .then(({ suggestions }) => setSeeds(suggestions))
      .catch(() => setSeeds([]));
  }, [id]);

  const remove = useCallback(
    async (broaderId: number) => {
      setBusy(true);
      try {
        await rejectBroaderOf(id, broaderId);
        await load();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [id, load],
  );

  const confirm = useCallback(
    async (broaderId: number) => {
      setBusy(true);
      try {
        await confirmBroaderOf(id, broaderId);
        await load();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [id, load],
  );

  const excludeIds = new Set([
    ...(concept?.broader_parents ?? []).map((p) => p.id),
    ...(concept?.proposed_parents ?? []).map((p) => p.id),
  ]);

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
            <a href={`/claims?concept_id=${concept.id}`}>Browse its Claims →</a>{" "}
            <a href={`/concepts/${concept.id}/neighbourhood`}>Explore its neighbourhood →</a>
          </p>

          {/* Hierarchy (broader-of): the taxonomic parents this Concept rolls up
              under. Confirmed parents participate in roll-up; proposals wait for a
              one-click confirm. Attaching here lands confirmed immediately. */}
          <div className="connections">
            <h3>Rolls up under (hierarchy)</h3>

            {concept.broader_parents.length === 0 &&
              concept.proposed_parents.length === 0 && (
                <p className="muted">No broader parent yet.</p>
              )}

            {concept.broader_parents.map((p) => (
              <span key={p.id} className="link-pill">
                <a href={`/concepts/${p.id}`}>{p.name}</a>{" "}
                <button
                  className="unlink"
                  disabled={busy}
                  title="Remove this parent"
                  onClick={() => remove(p.id)}
                >
                  ✕
                </button>
              </span>
            ))}

            {concept.proposed_parents.map((p) => (
              <span key={p.id} className="link-pill proposed">
                <a href={`/concepts/${p.id}`}>{p.name}</a>{" "}
                <span className="muted">proposed</span>{" "}
                <button
                  className="mini"
                  disabled={busy}
                  title="Confirm this proposed parent (visible to roll-up)"
                  onClick={() => confirm(p.id)}
                >
                  confirm
                </button>{" "}
                <button
                  className="unlink"
                  disabled={busy}
                  title="Reject this proposal"
                  onClick={() => remove(p.id)}
                >
                  ✕
                </button>
              </span>
            ))}

            <div style={{ marginTop: "0.5rem" }}>
              <AttachParent
                narrowerId={concept.id}
                catalogue={catalogue}
                excludeIds={excludeIds}
                seeds={seeds}
                onAttached={load}
              />
            </div>
          </div>

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
