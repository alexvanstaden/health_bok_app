"use client";

// Browse the Concepts of the Body of Knowledge (issue #14): the normalized,
// deduplicated hubs that Claims and Protocols reference (CONTEXT.md "Concept").
// Filterable by name; each row links to the Concept's detail, the pivot point for
// relatedness-by-shared-Concept — everything that references it, without a visual
// graph (ADR-0009).
//
// Concepts are the one entity that MAY be merged/normalized (CONTEXT.md). Beyond
// the automatic de-duplication (ADR-0014), the owner can *manually* merge here
// (issue #86): multi-select 2+ hubs, pick the survivor, optionally rename it, and
// everything referencing the merged-away hubs re-points to the survivor.

import { useCallback, useEffect, useMemo, useState } from "react";
import { BokConcept, listConcepts, mergeConcepts } from "../lib/api";

export default function ConceptsList() {
  const [concepts, setConcepts] = useState<BokConcept[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [merging, setMerging] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { concepts } = await listConcepts({});
      setConcepts(concepts);
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

  const shown = useMemo(
    () => concepts.filter((c) => c.name.toLowerCase().includes(search.toLowerCase())),
    [concepts, search],
  );

  const toggle = useCallback((id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectedConcepts = useMemo(
    () => concepts.filter((c) => selected.has(c.id)),
    [concepts, selected],
  );

  const onMerged = useCallback(async () => {
    setMerging(false);
    setSelected(new Set());
    await refresh();
  }, [refresh]);

  return (
    <>
      <h1>Concepts</h1>
      <p className="subtitle">
        The normalized hubs Claims and Protocols reference — {shown.length} shown.
      </p>
      <div className="filters">
        <input
          className="search"
          placeholder="Filter by name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {error && <p className="error">API error: {error}</p>}

      {selected.size > 0 && (
        <div className="merge-bar">
          <span className="muted">
            {selected.size} selected
          </span>
          <button
            className="primary"
            disabled={selected.size < 2}
            onClick={() => setMerging(true)}
          >
            Merge…
          </button>
          <button className="link" onClick={() => setSelected(new Set())}>
            Clear
          </button>
          {selected.size < 2 && (
            <span className="muted">select 2+ to merge</span>
          )}
        </div>
      )}

      {loaded && shown.length === 0 && !error && <p className="muted">No Concepts match.</p>}

      {shown.map((c) => (
        <div key={c.id} className={`select-row${selected.has(c.id) ? " selected" : ""}`}>
          <input
            type="checkbox"
            aria-label={`Select ${c.name} to merge`}
            checked={selected.has(c.id)}
            onChange={() => toggle(c.id)}
          />
          <div className="grow">
            <a href={`/concepts/${c.id}`}>
              <div className="row">
                <strong>{c.name}</strong>
                {c.kind && <span className="badge">{c.kind}</span>}
                <span className="spacer" />
                <span className="muted">
                  {c.reference_count} ref{c.reference_count === 1 ? "" : "s"}
                </span>
              </div>
            </a>
          </div>
        </div>
      ))}

      {merging && (
        <MergeDialog
          concepts={selectedConcepts}
          onClose={() => setMerging(false)}
          onMerged={onMerged}
        />
      )}
    </>
  );
}

// The merge dialog: pick which selected Concept survives, optionally rename it, and
// confirm. On confirm every other selected Concept folds onto the survivor and is
// deleted — the dialog states this plainly since the merge is irreversible. A merge
// that would close a broader-of cycle fails whole; the API's error surfaces here.
function MergeDialog({
  concepts,
  onClose,
  onMerged,
}: {
  concepts: BokConcept[];
  onClose: () => void;
  onMerged: () => void;
}) {
  const [survivorId, setSurvivorId] = useState(concepts[0]?.id ?? 0);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const survivor = concepts.find((c) => c.id === survivorId);
  const mergedAway = concepts.filter((c) => c.id !== survivorId);
  const survivingName = newName.trim() || survivor?.name || "";

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await mergeConcepts({
        survivor_id: survivorId,
        concept_ids: concepts.map((c) => c.id),
        new_name: newName.trim() || undefined,
      });
      onMerged();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="overlay" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Merge {concepts.length} Concepts</h2>
        <p className="muted">
          Everything referencing the other {mergedAway.length}{" "}
          Concept{mergedAway.length === 1 ? "" : "s"} — Claims, Protocols, Markers,
          Goal references, hierarchy and relationships — will re-point to{" "}
          <strong>{survivingName}</strong>, and the merged-away Concepts will be
          deleted. This cannot be undone.
        </p>

        <div className="survivor-pick">
          <span className="muted">Which Concept survives?</span>
          {concepts.map((c) => (
            <label key={c.id}>
              <input
                type="radio"
                name="survivor"
                checked={c.id === survivorId}
                onChange={() => setSurvivorId(c.id)}
              />
              <span>
                <strong>{c.name}</strong>{" "}
                <span className="muted">
                  ({c.reference_count} ref{c.reference_count === 1 ? "" : "s"})
                </span>
              </span>
            </label>
          ))}
        </div>

        <label className="form">
          <span>New name (optional)</span>
          <input
            placeholder={survivor?.name ?? "Keep current name"}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
        </label>

        {error && <p className="error">{error}</p>}

        <div className="dialog-actions">
          <button className="link" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="primary" onClick={confirm} disabled={busy}>
            {busy ? "Merging…" : `Merge into ${survivingName}`}
          </button>
        </div>
      </div>
    </div>
  );
}
