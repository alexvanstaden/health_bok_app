"use client";

// The backfill review queue (issue #15): a Creator's back-catalogue surfaced as
// metadata-only Candidates — thumbnail, title, description, publish date, link —
// with no preview tier. The owner judges relevance at a glance, opens YouTube if
// they need more, bulk-rejects obvious noise, or approves. Approving runs the very
// same pipeline as a daily Candidate (the worker transcribes-if-needed, then
// extracts and admits), so the queue polls to show each Candidate's state walk.

import { useCallback, useEffect, useState } from "react";
import {
  BackfillCandidate,
  approveCandidate,
  listBackfillCandidates,
  rejectBackfillCandidates,
} from "../lib/api";

export default function BackfillQueue() {
  const [candidates, setCandidates] = useState<BackfillCandidate[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { candidates } = await listBackfillCandidates();
      setCandidates(candidates);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, []);

  // Poll so worker-driven state transitions (approved → processing → admitted)
  // become visible, and approved/admitted Candidates drop out of the queue.
  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [refresh]);

  function toggle(videoId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(videoId) ? next.delete(videoId) : next.add(videoId);
      return next;
    });
  }

  async function approve(videoId: string) {
    setBusy(true);
    try {
      await approveCandidate(videoId);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function rejectSelected() {
    if (selected.size === 0) return;
    setBusy(true);
    try {
      await rejectBackfillCandidates([...selected]);
      setSelected(new Set());
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Backfill</h1>
      <p className="subtitle">
        Back-catalogue Candidates, by metadata only. Approve to admit, or bulk-reject noise.
      </p>

      <div className="row">
        <button
          className="danger"
          disabled={busy || selected.size === 0}
          onClick={rejectSelected}
        >
          Reject selected ({selected.size})
        </button>
      </div>

      {error && <p className="error">API error: {error}</p>}
      {loaded && candidates.length === 0 && !error && (
        <p className="muted">No backfill Candidates awaiting review. Trigger one from Creators.</p>
      )}

      {candidates.map((c) => (
        <section key={c.video_id} className="card">
          <div className="row backfill-row">
            <input
              type="checkbox"
              checked={selected.has(c.video_id)}
              onChange={() => toggle(c.video_id)}
              aria-label={`Select ${c.title}`}
            />
            <a href={c.url} target="_blank" rel="noreferrer" className="thumb">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={c.thumbnail_url} alt="" width={160} height={90} />
            </a>
            <div className="backfill-meta">
              <div className="row">
                <h2>{c.title}</h2>
                <span className="spacer" />
                {c.state !== "candidate" && (
                  <span className={`badge ${c.state}`}>{c.state}</span>
                )}
              </div>
              <p className="meta muted">
                {c.channel_name} · {new Date(c.published_at).toLocaleDateString()}
              </p>
              {c.description && <p className="summary">{c.description}</p>}
              <div className="row">
                <button
                  className="primary"
                  disabled={busy || c.state !== "candidate"}
                  onClick={() => approve(c.video_id)}
                >
                  Approve
                </button>
                <span className="spacer" />
                <a href={c.url} target="_blank" rel="noreferrer">
                  Open on YouTube
                </a>
              </div>
            </div>
          </div>
        </section>
      ))}
    </>
  );
}
