"use client";

// One Marker's history as a series (issue #16): every dated reading for a Concept,
// newest first, with "out of range" derived from each reading's stored reference
// range. This is the true time-series — readings are append-only, so the history
// only ever grows.

import { useCallback, useEffect, useState } from "react";
import { MarkerReading, getMarkerHistory } from "../../lib/api";

export default function MarkerHistory({
  params,
}: {
  params: { conceptId: string };
}) {
  const conceptId = Number(params.conceptId);
  const [readings, setReadings] = useState<MarkerReading[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const { readings } = await getMarkerHistory(conceptId);
      setReadings(readings);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [conceptId]);

  useEffect(() => {
    load();
  }, [load]);

  const name = readings[0]?.concept.name;

  return (
    <>
      <p>
        <a href="/markers">← All Markers</a>
      </p>
      {error && <p className="error">API error: {error}</p>}
      {loaded && readings.length === 0 && !error && (
        <p className="muted">No readings for this Marker.</p>
      )}

      {readings.length > 0 && (
        <>
          <h1>{name}</h1>
          <p className="subtitle">
            {readings.length} reading{readings.length === 1 ? "" : "s"}, newest first.{" "}
            <a href={`/concepts/${conceptId}`}>See the Concept ↗</a>
          </p>

          {readings.map((r) => (
            <div key={r.id} className="list-item">
              <div className="row">
                <strong>
                  {r.value} {r.unit}
                </strong>
                {r.out_of_range ? (
                  <span className="badge failed">out of range</span>
                ) : (
                  <span className="badge admitted">in range</span>
                )}
                <span className="spacer" />
                <span className="muted">{r.measured_at.slice(0, 10)}</span>
              </div>
              <div className="meta">{refLabel(r.reference_low, r.reference_high)}</div>
            </div>
          ))}
        </>
      )}
    </>
  );
}

function refLabel(low: number | null, high: number | null): string {
  if (low !== null && high !== null) return `reference range ${low}–${high}`;
  if (high !== null) return `reference range < ${high}`;
  if (low !== null) return `reference range > ${low}`;
  return "no reference range recorded";
}
