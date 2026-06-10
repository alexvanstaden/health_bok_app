"use client";

// Logs — a read-only record of every video Source the pipeline has processed into
// the Body of Knowledge, newest-first (issue #33). It is not a work surface: it has
// no actions. It exists so the owner can see, over time, what has been processed
// and confirm at a glance that a given video has already been handled and will not
// be reprocessed (the dedup guard already lives in the pipeline — this makes it
// visible). The BoK-state badge distinguishes what actually reached the Body of
// Knowledge (admitted) from what was processed but never admitted (failed / pending).
// Each row links through to that video's existing Claims page. The page is labelled
// "Logs" by the owner's explicit choice.

import { useEffect, useState } from "react";
import { ProcessedVideo, listVideos } from "../lib/api";

export default function LogsPage() {
  const [videos, setVideos] = useState<ProcessedVideo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    listVideos()
      .then(({ videos }) => {
        setVideos(videos);
        setError(null);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoaded(true));
  }, []);

  return (
    <>
      <h1>Logs</h1>
      <p className="subtitle">
        Every video processed into the Body of Knowledge — the system never
        reprocesses one twice. {videos.length} shown.
      </p>

      {error && <p className="error">API error: {error}</p>}
      {loaded && !error && videos.length === 0 && (
        <p className="muted">
          Nothing processed yet. Videos appear here once the daily pipeline has
          archived a Transcript and Summary for them.
        </p>
      )}

      {videos.map((v) => (
        <a
          key={v.video_id}
          href={`/videos/${v.video_id}/claims`}
          className="list-item"
        >
          <div className="row">
            <strong>{v.creator}</strong>
            <span className={`badge ${v.bok_state}`}>{v.bok_state}</span>
            <span className="spacer" />
            <span className="muted">{formatDate(v.added_at)}</span>
          </div>
          <div className="meta">{v.summary}</div>
        </a>
      ))}
    </>
  );
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
