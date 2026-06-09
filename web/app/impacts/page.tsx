"use client";

// The Impact inbox — change detection's read/act surface (issue #18). New evidence
// (a just-admitted Claim/Protocol) and new choices (a recorded Decision/Goal) raise
// stance-typed Impacts against the owner's Decisions, Goals, and Markers. Here the
// owner filters by stance and anchor, then walks each Impact new → reviewed →
// actioned | dismissed so it never re-nags. Actioning records the Decision revised
// or created in response; a burst can be bulk-dismissed. Detection, dedup, and the
// lifecycle are enforced server-side — this is just the inbox over them.

import { useCallback, useEffect, useState } from "react";
import {
  Decision,
  Impact,
  actionImpact,
  bulkDismissImpacts,
  dismissImpact,
  listDecisions,
  listImpacts,
  reviewImpact,
} from "../lib/api";

const STANCES = ["reinforces", "contradicts", "refines", "opportunity"] as const;
// The inbox defaults to the unresolved Impacts (those still nagging); an explicit
// state lets the owner look back at what was actioned or dismissed.
const STATES = ["open", "new", "reviewed", "actioned", "dismissed"] as const;

export default function ImpactsPage() {
  const [impacts, setImpacts] = useState<Impact[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [stance, setStance] = useState<string>("");
  const [state, setState] = useState<string>("open");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      // "open" is the default unresolved inbox: send no state filter.
      const { impacts } = await listImpacts({
        stance: stance || undefined,
        state: state === "open" ? undefined : state,
      });
      setImpacts(impacts);
      setSelected(new Set());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [stance, state]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    // The Decision picker for actioning an Impact — loaded once.
    listDecisions()
      .then(({ decisions }) => setDecisions(decisions))
      .catch(() => setDecisions([]));
  }, []);

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  const selectable = impacts.filter((i) => i.state === "new" || i.state === "reviewed");

  return (
    <>
      <div className="row">
        <h1>Impacts</h1>
        <span className="spacer" />
        {selected.size > 0 && (
          <button
            className="link-pill"
            onClick={() => act(() => bulkDismissImpacts([...selected]))}
          >
            Dismiss selected ({selected.size})
          </button>
        )}
      </div>
      <p className="subtitle">
        Change detection: when new evidence bears on what you do, want, or measure —
        or a new choice meets your library — it shows up here. {impacts.length} shown.
      </p>

      <div className="filters">
        <label>
          Stance{" "}
          <select value={stance} onChange={(e) => setStance(e.target.value)}>
            <option value="">all</option>
            {STANCES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          State{" "}
          <select value={state} onChange={(e) => setState(e.target.value)}>
            {STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && <p className="error">API error: {error}</p>}
      {loaded && impacts.length === 0 && !error && (
        <p className="muted">
          Nothing here. Impacts appear as evidence is admitted and you record Goals,
          Markers, and Decisions.
        </p>
      )}

      {impacts.map((i) => (
        <ImpactRow
          key={i.id}
          impact={i}
          decisions={decisions}
          checked={selected.has(i.id)}
          onToggle={() => toggle(i.id)}
          onReview={() => act(() => reviewImpact(i.id))}
          onDismiss={() => act(() => dismissImpact(i.id))}
          onAction={(decisionId) => act(() => actionImpact(i.id, decisionId))}
        />
      ))}

      {selectable.length === 0 && impacts.length > 0 && (
        <p className="muted">These are resolved — switch State to “open” for the inbox.</p>
      )}
    </>
  );
}

function ImpactRow({
  impact,
  decisions,
  checked,
  onToggle,
  onReview,
  onDismiss,
  onAction,
}: {
  impact: Impact;
  decisions: Decision[];
  checked: boolean;
  onToggle: () => void;
  onReview: () => void;
  onDismiss: () => void;
  onAction: (decisionId: number) => void;
}) {
  const [acting, setActing] = useState(false);
  const [decisionId, setDecisionId] = useState<number | "">("");
  const resolved = impact.state === "actioned" || impact.state === "dismissed";

  return (
    <div className="list-item">
      <div className="row">
        {!resolved && (
          <input type="checkbox" checked={checked} onChange={onToggle} aria-label="select" />
        )}
        <span className={`badge stance-${impact.stance}`}>{impact.stance}</span>
        <span className="spacer" />
        <span className="muted">{impact.state}</span>
      </div>
      <div style={{ marginTop: 6 }}>
        New {impact.source.type}: <strong>{impact.source.label}</strong>
      </div>
      <div className="meta">
        → your {impact.anchor.type}: <strong>{impact.anchor.label}</strong>
      </div>
      {impact.detail && <div className="meta">{impact.detail}</div>}

      {!resolved && (
        <div className="row" style={{ marginTop: 8 }}>
          {impact.state === "new" && (
            <button onClick={onReview}>Mark reviewed</button>
          )}
          <button onClick={onDismiss}>Dismiss</button>
          {acting ? (
            <>
              <select
                value={decisionId}
                onChange={(e) =>
                  setDecisionId(e.target.value ? Number(e.target.value) : "")
                }
              >
                <option value="">choose a Decision…</option>
                {decisions.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.action}
                  </option>
                ))}
              </select>
              <button
                className="primary"
                disabled={decisionId === ""}
                onClick={() => decisionId !== "" && onAction(decisionId)}
              >
                Record
              </button>
              <button onClick={() => setActing(false)}>Cancel</button>
            </>
          ) : (
            <button onClick={() => setActing(true)}>Action…</button>
          )}
        </div>
      )}

      {impact.state === "actioned" && impact.actioned_decision_id && (
        <div className="meta">
          Actioned →{" "}
          <a href={`/decisions/${impact.actioned_decision_id}`}>the Decision you recorded</a>
        </div>
      )}
    </div>
  );
}
