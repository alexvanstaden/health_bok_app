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
// detach an attached one. Each persists as a `references` edge. This is the
// attach/detach the suggest-then-confirm slices that follow will call.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  BokConcept,
  Goal,
  attachGoalConcept,
  deleteGoal,
  detachGoalConcept,
  getGoal,
  listConcepts,
} from "../../lib/api";

export default function GoalDetail({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const router = useRouter();
  const [goal, setGoal] = useState<Goal | null>(null);
  const [catalogue, setCatalogue] = useState<BokConcept[]>([]);
  const [term, setTerm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [g, { concepts }] = await Promise.all([
        getGoal(id),
        listConcepts({}),
      ]);
      setGoal(g);
      setCatalogue(concepts);
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
          </div>
        </>
      )}
    </>
  );
}
