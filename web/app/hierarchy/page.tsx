"use client";

// The broader-of review queue (ADR-0014): the taxonomy links the two-tier auto
// path proposed but did not confirm outright — a parent it judged plausible but not
// close enough to organize the graph on its own. Each row is one proposed
// `broader → narrower` edge the owner confirms (making it visible to roll-up) or
// rejects, so a wrong guess never silently corrupts a subtree. Confident links are
// auto-confirmed elsewhere and never appear here.

import { useCallback, useEffect, useState } from "react";
import {
  BroaderOfProposal,
  confirmBroaderOf,
  getBroaderOfProposals,
  rejectBroaderOf,
} from "../lib/api";

export default function HierarchyReview() {
  const [proposals, setProposals] = useState<BroaderOfProposal[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const { proposals } = await getBroaderOfProposals();
      setProposals(proposals);
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

  const act = useCallback(
    async (p: BroaderOfProposal, decision: "confirm" | "reject") => {
      const key = `${p.broader_id}>${p.narrower_id}`;
      setBusy(key);
      try {
        if (decision === "confirm") {
          await confirmBroaderOf(p.narrower_id, p.broader_id);
        } else {
          await rejectBroaderOf(p.narrower_id, p.broader_id);
        }
        // Drop the acted-on row without a full refetch, so the list stays put.
        setProposals((rows) =>
          rows.filter(
            (r) =>
              !(r.broader_id === p.broader_id && r.narrower_id === p.narrower_id),
          ),
        );
        setError(null);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  return (
    <>
      <h1>Hierarchy review</h1>
      <p className="subtitle">
        Proposed <em>broader-of</em> links awaiting your call — {proposals.length}{" "}
        pending, closest match first. Confident links are organized automatically;
        these are the ones the system was less sure about. The{" "}
        <strong>score</strong> is the distance between the two Concepts&rsquo;
        embeddings — lower is a closer, more confident match. Confirming one makes it
        visible to roll-up (so the narrower Concept shows up in the broader one&rsquo;s
        neighbourhood).
      </p>

      {error && <p className="error">API error: {error}</p>}
      {loaded && proposals.length === 0 && !error && (
        <p className="muted">Nothing to review — the queue is empty.</p>
      )}

      {proposals.map((p) => {
        const key = `${p.broader_id}>${p.narrower_id}`;
        return (
          <div key={key} className="list-item">
            <div className="row">
              <a href={`/concepts/${p.narrower_id}`}>
                <strong>{p.narrower_name}</strong>
              </a>
              <span className="muted">rolls up under</span>
              <a href={`/concepts/${p.broader_id}`}>
                <strong>{p.broader_name}</strong>
              </a>
              <span className="muted" title="cosine distance — lower is a closer match">
                score {p.distance != null ? p.distance.toFixed(2) : "—"}
              </span>
              <span className="spacer" />
              <button
                type="button"
                disabled={busy === key}
                onClick={() => act(p, "confirm")}
              >
                Confirm
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy === key}
                onClick={() => act(p, "reject")}
              >
                Reject
              </button>
            </div>
          </div>
        );
      })}
    </>
  );
}
