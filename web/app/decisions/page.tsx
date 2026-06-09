"use client";

// Record and browse Decisions — the owner's time-bound adoptions of interventions
// (CONTEXT.md "Decision"), issue #16. A Decision carries its *own* actual
// parameters; open one to review its rationale (the Protocol it implements, the
// Goals it serves, the Markers that motivated it, the Claims that support it) and
// confirm Concept-overlap suggestions. New Decisions are recorded on their own page
// — or by "adopting" a Protocol from its detail view.

import { useCallback, useEffect, useState } from "react";
import { Decision, listDecisions } from "../lib/api";

function paramsLine(d: Decision): string {
  return [
    d.dose && `dose: ${d.dose}`,
    d.timing && `timing: ${d.timing}`,
    d.frequency && `frequency: ${d.frequency}`,
    d.duration && `duration: ${d.duration}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

export default function DecisionsPage() {
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { decisions } = await listDecisions();
      setDecisions(decisions);
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

  return (
    <>
      <div className="row">
        <h1>Decisions</h1>
        <span className="spacer" />
        <a href="/decisions/new" className="link-pill">
          + New decision
        </a>
      </div>
      <p className="subtitle">
        What you've adopted, with your own actual parameters — {decisions.length}{" "}
        recorded.
      </p>

      {error && <p className="error">API error: {error}</p>}
      {loaded && decisions.length === 0 && !error && (
        <p className="muted">
          No Decisions yet. Record one, or adopt a Protocol from its detail page.
        </p>
      )}

      {decisions.map((d) => (
        <a key={d.id} href={`/decisions/${d.id}`} className="list-item">
          <div className="row">
            <strong>{d.action}</strong>
            <span className="spacer" />
            <span className="muted">since {d.started_at.slice(0, 10)}</span>
          </div>
          {paramsLine(d) && <div className="meta">{paramsLine(d)}</div>}
          {d.concepts.length > 0 && (
            <div className="meta">{d.concepts.map((c) => c.name).join(", ")}</div>
          )}
        </a>
      ))}
    </>
  );
}
