"use client";

// Record and browse Markers — objective, dated readings the owner records, one
// time-series per referenced Concept (CONTEXT.md "Marker"), issue #16. Recording a
// reading is append-only: every entry is a new dated snapshot, never an overwrite.
// "Out of range" is derived from the reference range you enter, not hand-flagged.
// Each row opens that Concept's full history as a series.

import { useCallback, useEffect, useState } from "react";
import { MarkerSeries, createMarker, listMarkers } from "../lib/api";

const EMPTY = {
  concept: "",
  value: "",
  unit: "",
  reference_low: "",
  reference_high: "",
  measured_at: new Date().toISOString().slice(0, 10),
};

export default function MarkersPage() {
  const [series, setSeries] = useState<MarkerSeries[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(EMPTY);

  const refresh = useCallback(async () => {
    try {
      const { markers } = await listMarkers();
      setSeries(markers);
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

  function set(k: keyof typeof EMPTY, v: string) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!form.concept.trim() || form.value === "" || !form.unit.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await createMarker({
        concept: form.concept.trim(),
        value: Number(form.value),
        unit: form.unit.trim(),
        reference_low: form.reference_low === "" ? null : Number(form.reference_low),
        reference_high: form.reference_high === "" ? null : Number(form.reference_high),
        measured_at: `${form.measured_at}T00:00:00Z`,
      });
      setForm({ ...EMPTY, measured_at: form.measured_at });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Markers</h1>
      <p className="subtitle">
        Dated readings, one time-series per Concept — {series.length} tracked.
        Readings are append-only; record a new one to update.
      </p>

      <form className="form" onSubmit={add}>
        <label>
          Concept
          <input
            placeholder="apoB"
            value={form.concept}
            onChange={(e) => set("concept", e.target.value)}
          />
        </label>
        <div className="grid2">
          <label>
            Value
            <input
              type="number"
              step="any"
              value={form.value}
              onChange={(e) => set("value", e.target.value)}
            />
          </label>
          <label>
            Unit
            <input
              placeholder="mg/dL"
              value={form.unit}
              onChange={(e) => set("unit", e.target.value)}
            />
          </label>
          <label>
            Reference low (optional)
            <input
              type="number"
              step="any"
              value={form.reference_low}
              onChange={(e) => set("reference_low", e.target.value)}
            />
          </label>
          <label>
            Reference high (optional)
            <input
              type="number"
              step="any"
              value={form.reference_high}
              onChange={(e) => set("reference_high", e.target.value)}
            />
          </label>
        </div>
        <label>
          Measured on
          <input
            type="date"
            value={form.measured_at}
            onChange={(e) => set("measured_at", e.target.value)}
          />
        </label>
        <div className="row">
          <button
            className="primary"
            disabled={busy || !form.concept.trim() || form.value === "" || !form.unit.trim()}
          >
            {busy ? "Recording…" : "Record reading"}
          </button>
        </div>
      </form>

      {error && <p className="error">API error: {error}</p>}
      {loaded && series.length === 0 && !error && (
        <p className="muted">No Markers recorded yet.</p>
      )}

      {series.map((s) => (
        <a key={s.concept.id} href={`/markers/${s.concept.id}`} className="list-item">
          <div className="row">
            <strong>{s.concept.name}</strong>
            <span>
              {s.latest.value} {s.latest.unit}
            </span>
            {s.out_of_range && <span className="badge failed">out of range</span>}
            <span className="spacer" />
            <span className="muted">
              {s.reading_count} reading{s.reading_count === 1 ? "" : "s"}
            </span>
          </div>
          <div className="meta">
            latest {s.latest.measured_at.slice(0, 10)}
            {refLabel(s.latest.reference_low, s.latest.reference_high)}
          </div>
        </a>
      ))}
    </>
  );
}

function refLabel(low: number | null, high: number | null): string {
  if (low !== null && high !== null) return ` · range ${low}–${high}`;
  if (high !== null) return ` · range < ${high}`;
  if (low !== null) return ` · range > ${low}`;
  return "";
}
