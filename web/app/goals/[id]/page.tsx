"use client";

// A Goal's detail (issue #16): the Concepts it concerns and the Decisions that
// serve it — each a navigable link. A Goal nothing serves is shown *unmet*, so the
// gap between what the owner wants and what they're doing is visible. Delete it in
// place; its edges go with it.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Goal, deleteGoal, getGoal } from "../../lib/api";

export default function GoalDetail({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const router = useRouter();
  const [goal, setGoal] = useState<Goal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setGoal(await getGoal(id));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

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
                <a key={c.id} href={`/concepts/${c.id}`} className="link-pill">
                  {c.name}
                </a>
              ))
            )}
          </div>
        </>
      )}
    </>
  );
}
