"use client";

// The candidate review queue — the Web App's first real view (ADR-0007). The
// owner reviews each daily Candidate alongside its Summary and approves it into
// the Body of Knowledge, rejects it, or retries a failed extraction. Approval
// returns immediately; the worker does the slow admit, so the queue polls to show
// each Candidate walk approved → processing → admitted.

import { useCallback, useEffect, useState } from "react";
import {
  Candidate,
  approveCandidate,
  listCandidates,
  rejectCandidate,
  retryCandidate,
} from "./lib/api";

export default function ReviewQueue() {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { candidates } = await listCandidates();
      setCandidates(candidates);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, []);

  // Poll so worker-driven state transitions become visible without a reload.
  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [refresh]);

  async function act(
    videoId: string,
    fn: (id: string) => Promise<unknown>,
  ) {
    setBusy(videoId);
    try {
      await fn(videoId);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <h1>Review queue</h1>
      <p className="subtitle">
        Daily Candidates awaiting your approval into the Body of Knowledge.
      </p>

      {error && <p className="error">API error: {error}</p>}
      {loaded && candidates.length === 0 && !error && (
        <p className="muted">Nothing to review right now.</p>
      )}

      {candidates.map((c) => (
        <section key={c.video_id} id={`candidate-${c.video_id}`} className="card">
          <div className="row">
            <h2>{c.title}</h2>
            <span className="spacer" />
            <span className={`badge ${c.state}`}>{c.state}</span>
          </div>
          <p className="summary">{c.summary}</p>
          <div className="row">
            <button
              className="primary"
              disabled={busy === c.video_id || c.state !== "candidate"}
              onClick={() => act(c.video_id, approveCandidate)}
            >
              Approve
            </button>
            {c.state === "failed" && (
              <button
                disabled={busy === c.video_id}
                onClick={() => act(c.video_id, retryCandidate)}
              >
                Retry
              </button>
            )}
            <button
              className="danger"
              disabled={busy === c.video_id}
              onClick={() => act(c.video_id, rejectCandidate)}
            >
              Reject
            </button>
            <span className="spacer" />
            <a href={`/videos/${c.video_id}/claims`}>View extracted claims →</a>
            <a href={c.url} target="_blank" rel="noreferrer">
              Source
            </a>
          </div>
        </section>
      ))}
    </>
  );
}
