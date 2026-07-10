"use client";

// Attach a broader-of parent by hand (issue #87). Hierarchy is the one Concept→
// Concept link the owner curates (ADR-0013): picking a parent here lands it
// *confirmed* immediately — visible to roll-up, no trip through the /hierarchy
// review queue. Shared by the /concepts list (inline per row) and the Concept
// detail page, so both file a Concept under a parent through the same machinery.
//
// Only existing Concepts are offered (broader-of links the catalogue, never mints):
// the owner picks from a datalist of every Concept not itself and not already a
// parent. A cycle-closing attach is rejected server-side with a 409, surfaced here.

import { useMemo, useState } from "react";
import { BokConcept, ConceptRef, attachBroaderOf } from "../lib/api";

export default function AttachParent({
  narrowerId,
  catalogue,
  excludeIds,
  onAttached,
  seeds = [],
  autoFocus = false,
}: {
  narrowerId: number;
  catalogue: BokConcept[]; // every Concept, to resolve a typed name to an id
  excludeIds: Set<number>; // self + already-attached/proposed parents
  onAttached: () => void;
  seeds?: ConceptRef[]; // optional one-click parent suggestions (detail page)
  autoFocus?: boolean;
}) {
  const [term, setTerm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const attachable = useMemo(
    () => catalogue.filter((c) => c.id !== narrowerId && !excludeIds.has(c.id)),
    [catalogue, narrowerId, excludeIds],
  );

  const attach = async (broaderId: number) => {
    setBusy(true);
    setError(null);
    try {
      await attachBroaderOf(narrowerId, broaderId);
      setTerm("");
      onAttached();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const attachTyped = async () => {
    const wanted = term.trim().toLowerCase();
    if (!wanted) return;
    const match = attachable.find((c) => c.name.toLowerCase() === wanted);
    if (!match) {
      setError(`No Concept named “${term.trim()}” to roll up under.`);
      return;
    }
    await attach(match.id);
  };

  // Seeds not already attached, so a confirmed/proposed parent doesn't re-offer.
  const shownSeeds = seeds.filter((s) => !excludeIds.has(s.id) && s.id !== narrowerId);

  return (
    <div className="attach-parent">
      <div className="row">
        <input
          list={`parent-catalogue-${narrowerId}`}
          value={term}
          autoFocus={autoFocus}
          placeholder="Roll up under… pick a broader Concept"
          onChange={(e) => setTerm(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") attachTyped();
          }}
        />
        <datalist id={`parent-catalogue-${narrowerId}`}>
          {attachable.map((c) => (
            <option key={c.id} value={c.name} />
          ))}
        </datalist>
        <button disabled={busy || !term.trim()} onClick={attachTyped}>
          + parent
        </button>
      </div>

      {shownSeeds.length > 0 && (
        <div className="seeds">
          <span className="muted">Suggested:</span>{" "}
          {shownSeeds.map((s) => (
            <button
              key={s.id}
              className="link-pill"
              disabled={busy}
              title="Attach this broader parent (confirmed immediately)"
              onClick={() => attach(s.id)}
            >
              + {s.name}
            </button>
          ))}
        </div>
      )}

      {error && <p className="error">{error}</p>}
    </div>
  );
}
