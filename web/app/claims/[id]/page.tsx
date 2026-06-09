"use client";

// A Claim's detail (issue #14): its Source + locator deep-link, the Concepts it
// references and the Protocols it supports — each a navigable link traversing the
// `edges` (ADR-0008, ADR-0009 "no visual graph"). The owner edits it (recorded as
// a protected version, ADR-0010) or deletes it in place, curating opportunistically
// while browsing rather than via a standing review queue.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { BokClaim, deleteClaim, editClaim, getClaim } from "../../lib/api";

const TYPES = ["mechanism", "principle", "finding"];

export default function ClaimDetail({ params }: { params: { id: string } }) {
  const id = Number(params.id);
  const router = useRouter();
  const [claim, setClaim] = useState<BokClaim | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);

  const [text, setText] = useState("");
  const [type, setType] = useState("finding");
  const [locator, setLocator] = useState(0);

  const load = useCallback(async () => {
    try {
      const c = await getClaim(id);
      setClaim(c);
      setText(c.text);
      setType(c.type);
      setLocator(c.locator_seconds);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function save() {
    setBusy(true);
    try {
      await editClaim(id, { text, type, locator_seconds: locator });
      setEditing(false);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm("Delete this Claim? Its edges are removed too.")) return;
    setBusy(true);
    try {
      await deleteClaim(id);
      router.push("/claims");
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <>
      <p>
        <a href="/claims">← All Claims</a>
      </p>
      {error && <p className="error">API error: {error}</p>}
      {!claim && !error && <p className="muted">Loading…</p>}

      {claim && (
        <>
          <div className="row">
            <span className="badge">{claim.type}</span>
            {claim.protected && <span className="badge protected">edited</span>}
          </div>

          {!editing ? (
            <>
              <h1>{claim.text}</h1>
              <p className="subtitle">
                <a href={claim.deep_link} target="_blank" rel="noreferrer">
                  ▶ {claim.source.title} @ {claim.locator_seconds}s
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
                Claim text
                <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
              </label>
              <div className="grid2">
                <label>
                  Type
                  <select value={type} onChange={(e) => setType(e.target.value)}>
                    {TYPES.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Locator (seconds)
                  <input
                    type="number"
                    min={0}
                    value={locator}
                    onChange={(e) => setLocator(Number(e.target.value))}
                  />
                </label>
              </div>
              <div className="row">
                <button className="primary" disabled={busy} onClick={save}>
                  Save (protects this Claim)
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
            <h3>References Concepts</h3>
            {claim.concepts.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              claim.concepts.map((c) => (
                <a key={c.id} href={`/concepts/${c.id}`} className="link-pill">
                  {c.name}
                </a>
              ))
            )}

            <h3>Supports Protocols</h3>
            {claim.supports.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              claim.supports.map((p) => (
                <a key={p.id} href={`/protocols/${p.id}`} className="link-pill">
                  {p.action}
                </a>
              ))
            )}
          </div>
        </>
      )}
    </>
  );
}
