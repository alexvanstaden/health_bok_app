"use client";

// The backfill review queue (issue #15): a Creator's back-catalogue surfaced as
// metadata-only Candidates — thumbnail, title, description, publish date, link —
// with no preview tier. The owner judges relevance at a glance, opens YouTube if
// they need more, bulk-rejects obvious noise, or approves. Approving runs the very
// same pipeline as a daily Candidate (the worker transcribes-if-needed, then
// extracts and admits), so the queue polls to show each Candidate's state walk.
//
// The cheap single-pass listing leaves a Candidate without a description and with
// only a best-effort publish date, so each row carries a "Fetch details" action
// that pulls the real description + accurate date on demand (issue #31), and the
// queue can be sorted by publish date, newest- or oldest-first.

import { useCallback, useEffect, useState } from "react";
import {
  BackfillCandidate,
  BackfillOrder,
  Creator,
  QueueFilters,
  approveBackfillCandidates,
  approveCandidate,
  fetchBackfillDetails,
  hasActiveFilters,
  listBackfillCandidates,
  listCreators,
  rejectBackfillCandidates,
} from "../lib/api";
import { QueueToolbar } from "../lib/QueueToolbar";

export default function BackfillQueue() {
  const [candidates, setCandidates] = useState<BackfillCandidate[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [order, setOrder] = useState<BackfillOrder>("newest");
  const [filters, setFilters] = useState<QueueFilters>({});
  const [creators, setCreators] = useState<Creator[]>([]);
  const [fetching, setFetching] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  // The creator multi-select is populated from the watch list once on mount.
  useEffect(() => {
    listCreators()
      .then(({ creators }) => setCreators(creators))
      .catch(() => {});
  }, []);

  // Filters and sort are both server-side and compose; `refresh` closes over both,
  // so the 3s poll re-subscribes on either change and preserves the active filters.
  const refresh = useCallback(async () => {
    try {
      const { candidates } = await listBackfillCandidates(order, filters);
      setCandidates(candidates);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [order, filters]);

  // Poll so worker-driven state transitions (approved → processing → admitted)
  // become visible, and approved/admitted Candidates drop out of the queue.
  // Re-subscribes when the sort order changes so the new order takes effect at once.
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

  // Reject a single Candidate inline (issue #73), the per-video mirror of "Reject
  // selected". One click, no confirmation — matching the one-click Approve; a
  // rejected backfill Candidate leaves the queue and won't resurface on re-backfill.
  async function reject(videoId: string) {
    setBusy(true);
    try {
      await rejectBackfillCandidates([videoId]);
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(videoId);
        return next;
      });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Lazily fetch one Candidate's real description + accurate date (issue #31). The
  // returned, updated Candidate is patched into the list in place; the poll would
  // also pick it up since it is persisted, but the immediate swap is snappier.
  async function fetchDetails(videoId: string) {
    setFetching((prev) => new Set(prev).add(videoId));
    try {
      const updated = await fetchBackfillDetails(videoId);
      setCandidates((prev) =>
        prev.map((c) => (c.video_id === videoId ? updated : c)),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setFetching((prev) => {
        const next = new Set(prev);
        next.delete(videoId);
        return next;
      });
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

  // Approve every checked Candidate in one gesture (issue #73), the bulk mirror of
  // per-video Approve. Idempotent server-side — already in-flight ones are skipped.
  async function approveSelected() {
    if (selected.size === 0) return;
    setBusy(true);
    try {
      await approveBackfillCandidates([...selected]);
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
          className="primary"
          disabled={busy || selected.size === 0}
          onClick={approveSelected}
        >
          Approve selected ({selected.size})
        </button>
        <button
          className="danger"
          disabled={busy || selected.size === 0}
          onClick={rejectSelected}
        >
          Reject selected ({selected.size})
        </button>
      </div>

      {/* The shared filter toolbar, with the queue's own sort slotted alongside. */}
      <QueueToolbar filters={filters} onChange={setFilters} creators={creators}>
        <label className="muted">
          Sort:{" "}
          <select
            value={order}
            onChange={(e) => setOrder(e.target.value as BackfillOrder)}
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
        </label>
      </QueueToolbar>

      {error && <p className="error">API error: {error}</p>}
      {loaded && candidates.length === 0 && !error && (
        <p className="muted">
          {hasActiveFilters(filters)
            ? "No backfill Candidates match these filters."
            : "No backfill Candidates awaiting review. Trigger one from Creators."}
        </p>
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
              {c.description ? (
                <p className="summary">{c.description}</p>
              ) : (
                <p className="summary muted">No description yet — fetch details to load it.</p>
              )}
              <div className="row">
                {/* A fresh Candidate is approvable; a failed one is re-approvable
                    so the owner can reprocess it (the worker re-runs the same
                    pipeline). approved/processing/admitted are in-flight or done. */}
                <button
                  className="primary"
                  disabled={busy || (c.state !== "candidate" && c.state !== "failed")}
                  onClick={() => approve(c.video_id)}
                >
                  {c.state === "failed" ? "Retry" : "Approve"}
                </button>
                <button
                  className="danger"
                  disabled={busy || (c.state !== "candidate" && c.state !== "failed")}
                  onClick={() => reject(c.video_id)}
                >
                  Reject
                </button>
                <button
                  disabled={fetching.has(c.video_id)}
                  onClick={() => fetchDetails(c.video_id)}
                >
                  {fetching.has(c.video_id) ? "Fetching…" : "Fetch details"}
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
