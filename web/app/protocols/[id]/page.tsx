"use client";

// A Protocol's detail (issue #14): its Source + locator deep-link, the Claims that
// justify it, and the Concepts it references — each a navigable link traversing the
// `edges` (ADR-0008). The owner edits it (a protected version, ADR-0010) or deletes
// it in place. The DB still enforces that an edit keeps at least one of
// dose/timing/frequency/duration — strip them all and it's no longer a Protocol.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { BokProtocol, deleteProtocol, editProtocol, getProtocol } from "../../lib/api";

const EMPTY = { action: "", dose: "", timing: "", frequency: "", duration: "", locator_seconds: 0 };

export default function ProtocolDetail({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const router = useRouter();
  const [protocol, setProtocol] = useState<BokProtocol | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(EMPTY);

  const load = useCallback(async () => {
    try {
      const p = await getProtocol(id);
      setProtocol(p);
      setForm({
        action: p.action,
        dose: p.dose ?? "",
        timing: p.timing ?? "",
        frequency: p.frequency ?? "",
        duration: p.duration ?? "",
        locator_seconds: p.locator_seconds,
      });
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  function set(k: keyof typeof EMPTY, v: string) {
    setForm((f) => ({ ...f, [k]: k === "locator_seconds" ? Number(v) : v }));
  }

  async function save() {
    setBusy(true);
    try {
      await editProtocol(id, {
        action: form.action,
        dose: form.dose || null,
        timing: form.timing || null,
        frequency: form.frequency || null,
        duration: form.duration || null,
        locator_seconds: form.locator_seconds,
      });
      setEditing(false);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm("Delete this Protocol? Its edges are removed too.")) return;
    setBusy(true);
    try {
      await deleteProtocol(id);
      router.push("/protocols");
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <>
      <p>
        <a href="/protocols">← All Protocols</a>
      </p>
      {error && <p className="error">API error: {error}</p>}
      {!protocol && !error && <p className="muted">Loading…</p>}

      {protocol && (
        <>
          {protocol.protected && (
            <div className="row">
              <span className="badge protected">edited</span>
            </div>
          )}

          {!editing ? (
            <>
              <h1>{protocol.action}</h1>
              <p className="subtitle">
                {[
                  protocol.dose && `dose: ${protocol.dose}`,
                  protocol.timing && `timing: ${protocol.timing}`,
                  protocol.frequency && `frequency: ${protocol.frequency}`,
                  protocol.duration && `duration: ${protocol.duration}`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              <p>
                <a href={protocol.deep_link} target="_blank" rel="noreferrer">
                  ▶ {protocol.source.title} @ {protocol.locator_seconds}s
                </a>
              </p>
              <div className="row">
                <button onClick={() => setEditing(true)}>Edit</button>
                <button className="danger" disabled={busy} onClick={remove}>
                  Delete
                </button>
              </div>
            </>
          ) : (
            <div className="form">
              <label>
                Action
                <input value={form.action} onChange={(e) => set("action", e.target.value)} />
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
              <label>
                Locator (seconds)
                <input
                  type="number"
                  min={0}
                  value={form.locator_seconds}
                  onChange={(e) => set("locator_seconds", e.target.value)}
                />
              </label>
              <p className="muted">
                Keep at least one of dose / timing / frequency / duration — a Protocol with
                no structure is just a Claim.
              </p>
              <div className="row">
                <button className="primary" disabled={busy} onClick={save}>
                  Save (protects this Protocol)
                </button>
                <button
                  disabled={busy}
                  onClick={() => {
                    setEditing(false);
                    load();
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          <div className="connections">
            <h3>Justified by Claims</h3>
            {protocol.justified_by.length === 0 ? (
              <p className="muted">None linked yet.</p>
            ) : (
              protocol.justified_by.map((c) => (
                <a key={c.id} href={`/claims/${c.id}`} className="link-pill">
                  {c.text}
                </a>
              ))
            )}

            <h3>References Concepts</h3>
            {protocol.concepts.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              protocol.concepts.map((c) => (
                <a key={c.id} href={`/concepts/${c.id}`} className="link-pill">
                  {c.name}
                </a>
              ))
            )}
          </div>
        </>
      )}
    </>
  );
}
