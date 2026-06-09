"use client";

// A Decision's detail (issue #16): its own actual parameters, and its full
// rationale — the Protocol it implements, the Goals it serves, the Markers that
// motivated it, and the Claims that support it, each a navigable link. The Web App
// suggests Protocols/Claims/Goals relevant by Concept overlap; the owner confirms
// or dismisses each one individually. A Marker reading is linked from a picker.
// Confirmed links can be detached, and the Decision deleted in place.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Decision,
  MarkerReading,
  SuggestedLink,
  deleteDecision,
  getDecision,
  getDecisionSuggestions,
  linkDecision,
  listMarkerReadings,
  unlinkDecision,
} from "../../lib/api";

const HREF: Record<string, (id: number) => string> = {
  protocol: (id) => `/protocols/${id}`,
  goal: (id) => `/goals/${id}`,
  claim: (id) => `/claims/${id}`,
};

function paramsLine(d: Decision): string {
  return [
    d.dose && `dose: ${d.dose}`,
    d.timing && `timing: ${d.timing}`,
    d.frequency && `frequency: ${d.frequency}`,
    d.duration && `duration: ${d.duration}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

export default function DecisionDetail({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const router = useRouter();
  const [decision, setDecision] = useState<Decision | null>(null);
  const [suggestions, setSuggestions] = useState<SuggestedLink[]>([]);
  const [readings, setReadings] = useState<MarkerReading[]>([]);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [marker, setMarker] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [d, { suggestions }, { readings }] = await Promise.all([
        getDecision(id),
        getDecisionSuggestions(id),
        listMarkerReadings(),
      ]);
      setDecision(d);
      setSuggestions(suggestions);
      setReadings(readings);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function confirmLink(s: SuggestedLink) {
    setBusy(true);
    try {
      await linkDecision(id, { target_type: s.target_type, target_id: s.target_id });
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function unlink(targetType: string, targetId: number) {
    setBusy(true);
    try {
      await unlinkDecision(id, targetType, targetId);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function linkMarker() {
    if (!marker) return;
    setBusy(true);
    try {
      await linkDecision(id, { target_type: "marker", target_id: Number(marker) });
      setMarker("");
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function dismiss(s: SuggestedLink) {
    setDismissed((prev) => new Set(prev).add(`${s.target_type}:${s.target_id}`));
  }

  async function remove() {
    if (!confirm("Delete this Decision? Its links are removed too.")) return;
    setBusy(true);
    try {
      await deleteDecision(id);
      router.push("/decisions");
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  const linkedMarkerIds = new Set(decision?.motivated_by.map((m) => m.id));
  const shownSuggestions = suggestions.filter(
    (s) => !dismissed.has(`${s.target_type}:${s.target_id}`),
  );
  const linkableReadings = readings.filter((r) => !linkedMarkerIds.has(r.id));

  return (
    <>
      <p>
        <a href="/decisions">← All Decisions</a>
      </p>
      {error && <p className="error">API error: {error}</p>}
      {!decision && !error && <p className="muted">Loading…</p>}

      {decision && (
        <>
          <h1>{decision.action}</h1>
          <p className="subtitle">
            {paramsLine(decision) || "no parameters recorded"} · since{" "}
            {decision.started_at.slice(0, 10)}
            {decision.ended_at && ` until ${decision.ended_at.slice(0, 10)}`}
          </p>
          {decision.note && <p>{decision.note}</p>}
          <div className="row">
            <button className="danger" disabled={busy} onClick={remove}>
              Delete
            </button>
          </div>

          {/* --- Rationale: every confirmed connection, each detachable. --- */}
          <div className="connections">
            <h3>Implements Protocol</h3>
            {decision.implements.length === 0 ? (
              <p className="muted">None linked.</p>
            ) : (
              decision.implements.map((p) => (
                <span key={p.id} className="link-pill">
                  <a href={`/protocols/${p.id}`}>{p.action}</a>{" "}
                  <button className="unlink" onClick={() => unlink("protocol", p.id)}>
                    ✕
                  </button>
                </span>
              ))
            )}

            <h3>Serves Goals</h3>
            {decision.serves.length === 0 ? (
              <p className="muted">None linked.</p>
            ) : (
              decision.serves.map((g) => (
                <span key={g.id} className="link-pill">
                  <a href={`/goals/${g.id}`}>{g.title}</a>{" "}
                  <button className="unlink" onClick={() => unlink("goal", g.id)}>
                    ✕
                  </button>
                </span>
              ))
            )}

            <h3>Supporting evidence (Claims)</h3>
            {decision.supported_by.length === 0 ? (
              <p className="muted">None linked.</p>
            ) : (
              decision.supported_by.map((c) => (
                <span key={c.id} className="link-pill">
                  <a href={`/claims/${c.id}`}>{c.text}</a>{" "}
                  <button className="unlink" onClick={() => unlink("claim", c.id)}>
                    ✕
                  </button>
                </span>
              ))
            )}

            <h3>Motivated by Markers</h3>
            {decision.motivated_by.length === 0 ? (
              <p className="muted">None linked.</p>
            ) : (
              decision.motivated_by.map((m) => (
                <span key={m.id} className="link-pill">
                  {m.concept}: {m.value} {m.unit} ({m.measured_at.slice(0, 10)}){" "}
                  <button className="unlink" onClick={() => unlink("marker", m.id)}>
                    ✕
                  </button>
                </span>
              ))
            )}
            {linkableReadings.length > 0 && (
              <div className="row" style={{ marginTop: "0.5rem" }}>
                <select value={marker} onChange={(e) => setMarker(e.target.value)}>
                  <option value="">Link a Marker that motivated this…</option>
                  {linkableReadings.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.concept.name}: {r.value} {r.unit} ({r.measured_at.slice(0, 10)})
                    </option>
                  ))}
                </select>
                <button disabled={busy || !marker} onClick={linkMarker}>
                  Add
                </button>
              </div>
            )}

            <h3>References Concepts</h3>
            {decision.concepts.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              decision.concepts.map((c) => (
                <a key={c.id} href={`/concepts/${c.id}`} className="link-pill">
                  {c.name}
                </a>
              ))
            )}
          </div>

          {/* --- Suggest-then-confirm by Concept overlap. --- */}
          <div className="connections">
            <h3>Suggested links (by Concept overlap)</h3>
            {shownSuggestions.length === 0 ? (
              <p className="muted">
                Nothing more to suggest — every overlapping Protocol, Claim, and Goal
                is linked or dismissed.
              </p>
            ) : (
              shownSuggestions.map((s) => (
                <div key={`${s.target_type}:${s.target_id}`} className="suggestion">
                  <div>
                    <span className="badge">{s.target_type}</span>{" "}
                    {HREF[s.target_type] ? (
                      <a href={HREF[s.target_type](s.target_id)}>{s.label}</a>
                    ) : (
                      s.label
                    )}
                    <div className="meta">shares: {s.shared_concepts.join(", ")}</div>
                  </div>
                  <div className="row">
                    <button className="primary" disabled={busy} onClick={() => confirmLink(s)}>
                      Confirm
                    </button>
                    <button disabled={busy} onClick={() => dismiss(s)}>
                      Reject
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </>
      )}
    </>
  );
}
