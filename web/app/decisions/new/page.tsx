"use client";

// Record a new Decision (issue #16). Reached blank from the Decisions list, or via
// "Adopt" on a Protocol's detail page (`?protocol=<id>`) — which pre-fills the
// action and parameters from that Protocol and links it as the one the Decision
// `implements`. The pre-filled parameters are the owner's to change: a Decision
// holds its *own* actual dose/timing, so any deviation from the Protocol is
// recorded as such. The Protocol's Concepts are inherited server-side, so the
// detail page can suggest relevant links straight away.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { createDecision, getProtocol } from "../../lib/api";

const EMPTY = {
  action: "",
  dose: "",
  timing: "",
  frequency: "",
  duration: "",
  started_at: new Date().toISOString().slice(0, 10),
  ended_at: "",
  note: "",
  concepts: "",
};

export default function NewDecision({
  searchParams,
}: {
  searchParams: { protocol?: string };
}) {
  const router = useRouter();
  const protocolId = searchParams.protocol ? Number(searchParams.protocol) : undefined;
  const [form, setForm] = useState(EMPTY);
  const [adopting, setAdopting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const prefill = useCallback(async () => {
    if (protocolId === undefined) return;
    try {
      const p = await getProtocol(protocolId);
      setAdopting(p.action);
      setForm((f) => ({
        ...f,
        action: p.action,
        dose: p.dose ?? "",
        timing: p.timing ?? "",
        frequency: p.frequency ?? "",
        duration: p.duration ?? "",
      }));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [protocolId]);

  useEffect(() => {
    prefill();
  }, [prefill]);

  function set(k: keyof typeof EMPTY, v: string) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!form.action.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const { id } = await createDecision({
        action: form.action.trim(),
        dose: form.dose.trim() || null,
        timing: form.timing.trim() || null,
        frequency: form.frequency.trim() || null,
        duration: form.duration.trim() || null,
        started_at: `${form.started_at}T00:00:00Z`,
        ended_at: form.ended_at ? `${form.ended_at}T00:00:00Z` : null,
        note: form.note.trim() || null,
        concepts: form.concepts.split(",").map((s) => s.trim()).filter(Boolean),
        implements_protocol_id: protocolId ?? null,
      });
      router.push(`/decisions/${id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <>
      <p>
        <a href="/decisions">← All Decisions</a>
      </p>
      <h1>New decision</h1>
      {adopting && (
        <p className="subtitle">
          Adopting Protocol: <strong>{adopting}</strong>. Parameters are pre-filled —
          change them to your own actuals; deviation is recorded.
        </p>
      )}
      {error && <p className="error">API error: {error}</p>}

      <form className="form" onSubmit={save}>
        <label>
          Action
          <input
            placeholder="Take rapamycin"
            value={form.action}
            onChange={(e) => set("action", e.target.value)}
          />
        </label>
        <div className="grid2">
          <label>
            Dose
            <input value={form.dose} onChange={(e) => set("dose", e.target.value)} />
          </label>
          <label>
            Timing
            <input value={form.timing} onChange={(e) => set("timing", e.target.value)} />
          </label>
          <label>
            Frequency
            <input value={form.frequency} onChange={(e) => set("frequency", e.target.value)} />
          </label>
          <label>
            Duration
            <input value={form.duration} onChange={(e) => set("duration", e.target.value)} />
          </label>
        </div>
        <div className="grid2">
          <label>
            Started on
            <input
              type="date"
              value={form.started_at}
              onChange={(e) => set("started_at", e.target.value)}
            />
          </label>
          <label>
            Ended on (optional)
            <input
              type="date"
              value={form.ended_at}
              onChange={(e) => set("ended_at", e.target.value)}
            />
          </label>
        </div>
        <label>
          Concepts (comma-separated){adopting && " — added on top of the Protocol's"}
          <input
            placeholder="rapamycin, mTOR"
            value={form.concepts}
            onChange={(e) => set("concepts", e.target.value)}
          />
        </label>
        <label>
          Note / rationale (optional)
          <textarea
            rows={2}
            value={form.note}
            onChange={(e) => set("note", e.target.value)}
          />
        </label>
        <div className="row">
          <button className="primary" disabled={busy || !form.action.trim()}>
            {busy ? "Recording…" : "Record Decision"}
          </button>
        </div>
      </form>
    </>
  );
}
