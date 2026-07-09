"use client";

// A Goal's detail (issue #16): the Concepts it concerns and the Decisions that
// serve it — each a navigable link. A Goal nothing serves is shown *unmet*, so the
// gap between what the owner wants and what they're doing is visible. Delete it in
// place; its edges go with it.
//
// The Concept set is editable here (issue #37): attach one by picking from the
// existing catalogue or typing a term that isn't in it — normalized server-side
// through the same ConceptNormalizer the create form and admit pipeline use, so the
// personal layer and the Body of Knowledge keep one canonical Concept set — and
// detach an attached one. Each persists as a `references` edge.
//
// The page also suggests Concepts the Goal likely concerns, of two kinds the owner
// can tell apart at a glance. *Existing* Concepts (issue #38) are inferred from the
// title + detail over pgvector — already in the catalogue, never minted, no LLM.
// *New* Concepts (issue #39) are proposed by an LLM and resolve to nothing in the
// catalogue — confirming one mints it. Both confirm through the same #37 attach (the
// confirm half of suggest-then-confirm); minting stays owner-confirmed.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  BokConcept,
  ConceptSuggestion,
  Goal,
  NewConceptSuggestion,
  attachGoalConcept,
  deleteGoal,
  detachGoalConcept,
  getGoal,
  goalConceptSuggestions,
  goalNewConceptSuggestions,
  listConcepts,
} from "../../lib/api";

export default function GoalDetail({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const router = useRouter();
  const [goal, setGoal] = useState<Goal | null>(null);
  const [catalogue, setCatalogue] = useState<BokConcept[]>([]);
  const [suggestions, setSuggestions] = useState<ConceptSuggestion[]>([]);
  const [newSuggestions, setNewSuggestions] = useState<NewConceptSuggestion[]>([]);
  const [term, setTerm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [g, { concepts }, { suggestions: sugg }, { suggestions: newSugg }] =
        await Promise.all([
          getGoal(id),
          listConcepts({}),
          goalConceptSuggestions(id),
          goalNewConceptSuggestions(id),
        ]);
      setGoal(g);
      setCatalogue(concepts);
      setSuggestions(sugg);
      setNewSuggestions(newSugg);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function addConcept() {
    const name = term.trim();
    if (!name) return;
    setBusy(true);
    try {
      await attachGoalConcept(id, name);
      setTerm("");
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // One-click confirm of a suggested Concept — existing (#38) or new (#39) alike.
  // Both go through the same attach the manual add uses (issue #37): an existing term
  // reuses its Concept, a new term mints one — so confirming is the only thing that
  // ever mints. The next load drops the confirmed term from whichever list it was in.
  async function confirmSuggestion(name: string) {
    setBusy(true);
    try {
      await attachGoalConcept(id, name);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function detach(conceptId: number) {
    setBusy(true);
    try {
      await detachGoalConcept(id, conceptId);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm("Delete this Goal? Its links are removed too.")) return;
    setBusy(true);
    try {
      await deleteGoal(id);
      router.push("/goals");
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  // The catalogue picker offers every Concept not already attached; typing a term
  // not in the list mints (or reuses) one through the normalizer just the same.
  const attachedIds = new Set(goal?.concepts.map((c) => c.id));
  const attachable = catalogue.filter((c) => !attachedIds.has(c.id));

  return (
    <>
      <p>
        <a href="/goals">← All Goals</a>
      </p>
      {error && <p className="error">API error: {error}</p>}
      {!goal && !error && <p className="muted">Loading…</p>}

      {goal && (
        <>
          <h1>{goal.title}</h1>
          {goal.detail && <p className="subtitle">{goal.detail}</p>}
          <div className="row">
            <button className="danger" disabled={busy} onClick={remove}>
              Delete
            </button>
          </div>

          <div className="connections">
            <h3>Served by Decisions</h3>
            {goal.served_by.length === 0 ? (
              <p className="muted">
                Unmet — no Decision serves this Goal yet.
              </p>
            ) : (
              goal.served_by.map((d) => (
                <a key={d.id} href={`/decisions/${d.id}`} className="link-pill">
                  {d.action}
                </a>
              ))
            )}

            <h3>Concerns Concepts</h3>
            {goal.concepts.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              goal.concepts.map((c) => (
                <span key={c.id} className="link-pill">
                  <a href={`/concepts/${c.id}`}>{c.name}</a>{" "}
                  <button
                    className="unlink"
                    disabled={busy}
                    onClick={() => detach(c.id)}
                  >
                    ✕
                  </button>
                </span>
              ))
            )}

            {/* Attach a Concept: pick from the catalogue (datalist) or type a new
                term; both go through the same server-side normalizer. */}
            <div className="row" style={{ marginTop: "0.5rem" }}>
              <input
                list="concept-catalogue"
                value={term}
                placeholder="Add a Concept — pick or type a new term…"
                onChange={(e) => setTerm(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") addConcept();
                }}
              />
              <datalist id="concept-catalogue">
                {attachable.map((c) => (
                  <option key={c.id} value={c.name} />
                ))}
              </datalist>
              <button disabled={busy || !term.trim()} onClick={addConcept}>
                Add
              </button>
            </div>

            {/* Suggested existing Concepts (issue #38), inferred from the Goal's
                title + detail. Each is an existing Concept the Goal isn't already
                attached to — confirm one in a click to reuse it. Empty when nothing
                matches. Solid pills: reusing the vocabulary, nothing minted. */}
            {suggestions.length > 0 && (
              <div style={{ marginTop: "0.75rem" }}>
                <h4 className="muted">Suggested existing Concepts</h4>
                {suggestions.map((s) => (
                  <button
                    key={s.concept_id}
                    className="link-pill"
                    disabled={busy}
                    title={`cosine distance ${s.distance.toFixed(3)} — lower is a closer match`}
                    onClick={() => confirmSuggestion(s.name)}
                  >
                    + {s.name}{" "}
                    <span className="muted">{s.distance.toFixed(2)}</span>
                  </button>
                ))}
              </div>
            )}

            {/* Suggested NEW Concepts to mint (issue #39): proposed by an LLM and
                resolving to nothing in the catalogue. Set apart as dashed green pills
                — confirming one *grows* the vocabulary (it mints the Concept and
                attaches it), versus the solid pills above that only reuse. Minting
                stays owner-confirmed; empty when the LLM proposes nothing new (or
                fails — the existing suggestions above keep working). */}
            {newSuggestions.length > 0 && (
              <div style={{ marginTop: "0.75rem" }}>
                <h4 className="muted">Add new Concepts</h4>
                {newSuggestions.map((s) => (
                  <button
                    key={s.name}
                    className="link-pill new-concept"
                    disabled={busy}
                    title="Mint this new Concept and attach it"
                    onClick={() => confirmSuggestion(s.name)}
                  >
                    + add new: {s.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}
