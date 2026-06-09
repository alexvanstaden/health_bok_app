"use client";

// Record and browse Goals — the owner's stable intentions or risks (CONTEXT.md
// "Goal"), issue #16. Each Goal carries the Concepts it concerns (normalized like
// a Claim's), so it can overlap with Decisions; a Goal no Decision serves is
// flagged *unmet* — the prime target for an opportunity later. Open one to see
// which Decisions serve it.

import { useCallback, useEffect, useState } from "react";
import { Goal, createGoal, listGoals } from "../lib/api";

export default function GoalsPage() {
  const [goals, setGoals] = useState<Goal[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [detail, setDetail] = useState("");
  const [concepts, setConcepts] = useState("");

  const refresh = useCallback(async () => {
    try {
      const { goals } = await listGoals();
      setGoals(goals);
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

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await createGoal({
        title: title.trim(),
        detail: detail.trim() || null,
        concepts: concepts.split(",").map((s) => s.trim()).filter(Boolean),
      });
      setTitle("");
      setDetail("");
      setConcepts("");
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Goals</h1>
      <p className="subtitle">
        Stable intentions or risks you want to address — {goals.length} recorded.
      </p>

      <form className="form" onSubmit={add}>
        <label>
          Goal
          <input
            placeholder="Lower cardiovascular risk"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          Detail (optional)
          <input
            placeholder="Keep apoB and inflammation low"
            value={detail}
            onChange={(e) => setDetail(e.target.value)}
          />
        </label>
        <label>
          Concepts it concerns (comma-separated)
          <input
            placeholder="apoB, cardiovascular risk, rapamycin"
            value={concepts}
            onChange={(e) => setConcepts(e.target.value)}
          />
        </label>
        <div className="row">
          <button className="primary" disabled={busy || !title.trim()}>
            {busy ? "Recording…" : "Record Goal"}
          </button>
        </div>
      </form>

      {error && <p className="error">API error: {error}</p>}
      {loaded && goals.length === 0 && !error && (
        <p className="muted">No Goals recorded yet.</p>
      )}

      {goals.map((g) => (
        <a key={g.id} href={`/goals/${g.id}`} className="list-item">
          <div className="row">
            <strong>{g.title}</strong>
            <span className="spacer" />
            {g.served_by.length === 0 ? (
              <span className="badge failed">unmet</span>
            ) : (
              <span className="badge admitted">
                {g.served_by.length} decision{g.served_by.length === 1 ? "" : "s"}
              </span>
            )}
          </div>
          {g.detail && <div className="meta">{g.detail}</div>}
          {g.concepts.length > 0 && (
            <div className="meta">{g.concepts.map((c) => c.name).join(", ")}</div>
          )}
        </a>
      ))}
    </>
  );
}
