"use client";

// Logs — a read-only record of every video Source that reached a terminal admission,
// newest-first (issue #33). It is not a work surface: it has no actions. It exists so
// the owner can see, over time, what was carried into the Body of Knowledge and
// confirm at a glance that a given video has already been handled and will not be
// reprocessed. The BoK-state badge distinguishes what reached the Body of Knowledge
// (admitted) from what failed extraction (failed); videos still in flight or never
// approved are not listed. Each row links through to that video's existing Claims
// page. The page is labelled "Logs" by the owner's explicit choice.

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
        Every video admitted to the Body of Knowledge — and any that failed
        extraction. The system never reprocesses one twice. {videos.length} shown.
      </p>

      {error && <p className="error">API error: {error}</p>}
      {loaded && !error && videos.length === 0 && (
        <p className="muted">
          Nothing here yet. Videos appear once you approve them and the worker
          admits them to the Body of Knowledge (or extraction fails).
        </p>
      )}

      {videos.map((v) => (
        <a
          key={v.video_id}
          href={`/videos/${v.video_id}/claims`}
          className="list-item"
        >
          <div className="row">
            <strong>{v.title}</strong>
            <span className={`badge ${v.bok_state}`}>{v.bok_state}</span>
            <span className="spacer" />
            <span className="muted">{formatDate(v.added_at)}</span>
          </div>
          <div className="meta">{v.creator}</div>
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
