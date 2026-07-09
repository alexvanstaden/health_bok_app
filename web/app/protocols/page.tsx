"use client";

// Browse the Protocols of the Body of Knowledge (issue #14): structured,
// parameterized recommendations drawn from sources. Filterable by action text and,
// from the filter bar, by referenced Concept or by Goal (issue #84). The Concept
// filter narrows to Protocols that reference the Concept; the Goal filter is
// discovery-oriented — it shows Protocols whose Concepts overlap the Goal's attached
// Concepts, i.e. what the Body of Knowledge recommends for that Goal, adopted or not.
// Both filters live in the URL so a filtered view survives reload and can be linked
// to. Each row links to the Protocol's detail, where the owner sees its justifying
// Claims and edits/deletes it in place (ADR-0010).

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { BokConcept, BokProtocol, Goal, listConcepts, listGoals, listProtocols } from "../lib/api";

function paramLine(p: BokProtocol): string {
  return [
    p.dose && `dose: ${p.dose}`,
    p.timing && `timing: ${p.timing}`,
    p.frequency && `frequency: ${p.frequency}`,
    p.duration && `duration: ${p.duration}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

export default function ProtocolsList({
  searchParams,
}: {
  searchParams: { concept_id?: string; goal_id?: string };
}) {
  const router = useRouter();
  const conceptId = searchParams.concept_id ? Number(searchParams.concept_id) : undefined;
  const goalId = searchParams.goal_id ? Number(searchParams.goal_id) : undefined;
  const [protocols, setProtocols] = useState<BokProtocol[]>([]);
  const [concepts, setConcepts] = useState<BokConcept[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [search, setSearch] = useState("");

  const refresh = useCallback(async () => {
    try {
      const { protocols } = await listProtocols({ conceptId, goalId });
      setProtocols(protocols);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [conceptId, goalId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    // The Concept and Goal catalogues for the pickers — loaded once.
    listConcepts({}).then(({ concepts }) => setConcepts(concepts)).catch(() => {});
    listGoals().then(({ goals }) => setGoals(goals)).catch(() => {});
  }, []);

  const shown = useMemo(
    () => protocols.filter((p) => p.action.toLowerCase().includes(search.toLowerCase())),
    [protocols, search],
  );

  // One active graph filter at a time (issue #84): picking a Concept clears any Goal
  // and vice versa; picking "All" returns to the full list. Kept in the URL so the
  // view stays shareable and survives reload.
  const applyFilter = (params: { concept_id?: number; goal_id?: number }) => {
    const qs = new URLSearchParams();
    if (params.concept_id) qs.set("concept_id", String(params.concept_id));
    if (params.goal_id) qs.set("goal_id", String(params.goal_id));
    const query = qs.toString();
    router.push(query ? `/protocols?${query}` : "/protocols");
  };

  const selectedConcept = concepts.find((c) => c.id === conceptId);
  const selectedGoal = goals.find((g) => g.id === goalId);
  // A Goal with no attached Concepts can overlap nothing, so it lists nothing —
  // surface why, rather than an anonymous "no matches" (issue #84).
  const goalHasNoConcepts = selectedGoal !== undefined && selectedGoal.concepts.length === 0;

  return (
    <>
      <h1>Protocols</h1>
      <p className="subtitle">
        Structured recommendations drawn from sources — {shown.length} shown.
      </p>
      {selectedConcept && (
        <p className="crumb">
          Filtered to Concept <strong>{selectedConcept.name}</strong>.{" "}
          <a href="/protocols">Clear ✕</a>
        </p>
      )}
      {selectedGoal && (
        <p className="crumb">
          Filtered to Goal <strong>{selectedGoal.title}</strong>.{" "}
          <a href="/protocols">Clear ✕</a>
        </p>
      )}
      <div className="filters">
        <input
          className="search"
          placeholder="Filter by action…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          value={conceptId ?? ""}
          onChange={(e) =>
            applyFilter({ concept_id: e.target.value ? Number(e.target.value) : undefined })
          }
        >
          <option value="">All Concepts</option>
          {concepts.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <select
          value={goalId ?? ""}
          onChange={(e) =>
            applyFilter({ goal_id: e.target.value ? Number(e.target.value) : undefined })
          }
        >
          <option value="">All Goals</option>
          {goals.map((g) => (
            <option key={g.id} value={g.id}>
              {g.title}
            </option>
          ))}
        </select>
      </div>

      {error && <p className="error">API error: {error}</p>}
      {loaded && shown.length === 0 && !error && goalHasNoConcepts && (
        <p className="muted">
          This Goal has no attached Concepts yet, so nothing overlaps it. Attach
          Concepts to <a href={`/goals/${selectedGoal.id}`}>{selectedGoal.title}</a> to
          see what the Body of Knowledge recommends for it.
        </p>
      )}
      {loaded && shown.length === 0 && !error && !goalHasNoConcepts && (
        <p className="muted">No Protocols match.</p>
      )}

      {shown.map((p) => (
        <a key={p.id} href={`/protocols/${p.id}`} className="list-item">
          <div className="row">
            <strong>{p.action}</strong>
            {p.protected && <span className="badge protected">edited</span>}
          </div>
          <div className="meta">
            {paramLine(p)}
            {p.concepts.length > 0 && " · " + p.concepts.map((c) => c.name).join(", ")}
          </div>
        </a>
      ))}
    </>
  );
}
