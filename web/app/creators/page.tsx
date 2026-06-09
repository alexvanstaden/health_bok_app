"use client";

// Manage the watch list of Creators from the Web App, so the owner never needs
// the CLI to feed the pipeline (issue #15, ADR-0009). Add by @handle or channel
// URL (resolved once to a stable channel_id), see each Creator's resolved name,
// trigger a backfill of its back-catalogue, or remove it. An unresolvable
// reference fails loudly inline.

import { useCallback, useEffect, useState } from "react";
import {
  Creator,
  addCreator,
  listCreators,
  removeCreator,
  triggerBackfill,
} from "../lib/api";

export default function Creators() {
  const [creators, setCreators] = useState<Creator[]>([]);
  const [reference, setReference] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // channel_id or "add"
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { creators } = await listCreators();
      setCreators(creators);
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

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!reference.trim()) return;
    setBusy("add");
    setError(null);
    setNotice(null);
    try {
      const creator = await addCreator(reference.trim());
      setReference("");
      setNotice(`Added ${creator.name} — backfilled its recent back-catalogue.`);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function backfill(channelId: string) {
    setBusy(channelId);
    setError(null);
    setNotice(null);
    try {
      const { count } = await triggerBackfill(channelId);
      setNotice(
        count > 0
          ? `Backfill added ${count} new Candidate${count === 1 ? "" : "s"} — review them under Backfill.`
          : "Backfill found no new Candidates.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function remove(channelId: string) {
    setBusy(channelId);
    setError(null);
    setNotice(null);
    try {
      await removeCreator(channelId);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <h1>Creators</h1>
      <p className="subtitle">
        The watch list the daily pipeline follows. Add by @handle or channel URL.
      </p>

      <form className="row" onSubmit={add}>
        <input
          className="search"
          placeholder="@hubermanlab or https://www.youtube.com/@PeterAttiaMD"
          value={reference}
          onChange={(e) => setReference(e.target.value)}
        />
        <button className="primary" disabled={busy === "add" || !reference.trim()}>
          {busy === "add" ? "Adding…" : "Add Creator"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}
      {notice && <p className="muted">{notice}</p>}

      {loaded && creators.length === 0 && !error && (
        <p className="muted">No Creators on the watch list yet.</p>
      )}

      {creators.map((c) => (
        <section key={c.channel_id} className="card">
          <div className="row">
            <h2>{c.name}</h2>
            <span className="spacer" />
            <button disabled={busy === c.channel_id} onClick={() => backfill(c.channel_id)}>
              Backfill
            </button>
            <button
              className="danger"
              disabled={busy === c.channel_id}
              onClick={() => remove(c.channel_id)}
            >
              Remove
            </button>
          </div>
          <p className="meta muted">{c.channel_id}</p>
        </section>
      ))}
    </>
  );
}
