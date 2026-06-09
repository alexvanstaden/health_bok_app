// The single seam between the Web App and the Python HTTP API (ADR-0009).
// Everything the app does flows through here; the API reuses the health_bok
// domain over the one Postgres, so the Web App never re-implements logic.

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type Candidate = {
  video_id: string;
  title: string;
  url: string;
  summary: string;
  state: string;
  published_at: string;
};

export type Claim = {
  id: number;
  text: string;
  type: string;
  locator_seconds: number;
  deep_link: string;
  concepts: string[];
};

export type Protocol = {
  id: number;
  action: string;
  dose: string | null;
  timing: string | null;
  frequency: string | null;
  duration: string | null;
  locator_seconds: number;
  deep_link: string;
  concepts: string[];
};

export type VideoKnowledge = {
  video_id: string;
  state: string;
  claims: Claim[];
  protocols: Protocol[];
};

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store", ...init });
  if (!res.ok) {
    // Surface the API's `detail` when present (e.g. an unresolvable Creator), so
    // failures are loud and legible rather than a bare status code (issue #15).
    const detail = await res
      .json()
      .then((b) => (b && typeof b.detail === "string" ? b.detail : null))
      .catch(() => null);
    throw new Error(detail ?? `${init?.method ?? "GET"} ${path} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export function listCandidates(): Promise<{ candidates: Candidate[] }> {
  return json("/api/candidates");
}

export function approveCandidate(videoId: string) {
  return json(`/api/candidates/${videoId}/approve`, { method: "POST" });
}

export function rejectCandidate(videoId: string) {
  return json(`/api/candidates/${videoId}/reject`, { method: "POST" });
}

export function retryCandidate(videoId: string) {
  return json(`/api/candidates/${videoId}/retry`, { method: "POST" });
}

export function getVideoKnowledge(videoId: string): Promise<VideoKnowledge> {
  return json(`/api/videos/${videoId}/claims`);
}

// -- Creator management & backfill (issue #15) ------------------------------
// Maintain the watch list and pull in a Creator's back-catalogue from the Web
// App, so the owner never needs the CLI to feed the pipeline (ADR-0009). Adding
// reuses the resolve-once path and seeds recent Candidates; the explicit backfill
// trigger re-pulls on demand. Approving a backfill Candidate reuses approveCandidate
// — the worker then transcribes-if-needed before extracting.

export type Creator = { channel_id: string; name: string };

export type BackfillCandidate = {
  video_id: string;
  title: string;
  description: string;
  url: string;
  thumbnail_url: string;
  published_at: string;
  channel_id: string;
  channel_name: string;
  state: string;
};

export function listCreators(): Promise<{ creators: Creator[] }> {
  return json("/api/creators");
}

export function addCreator(reference: string): Promise<Creator> {
  return json("/api/creators", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reference }),
  });
}

export function removeCreator(channelId: string) {
  return json(`/api/creators/${channelId}`, { method: "DELETE" });
}

export function triggerBackfill(
  channelId: string,
): Promise<{ channel_id: string; stored: string[]; count: number }> {
  return json(`/api/creators/${channelId}/backfill`, { method: "POST" });
}

export function listBackfillCandidates(): Promise<{ candidates: BackfillCandidate[] }> {
  return json("/api/backfill");
}

export function rejectBackfillCandidates(videoIds: string[]): Promise<{ rejected: number }> {
  return json("/api/backfill/reject", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_ids: videoIds }),
  });
}

// -- The Body of Knowledge browser (issue #14) ------------------------------
// The browsable, editable evidence layer (ADR-0009 "no visual graph"). List and
// detail reads resolve connections server-side by traversing `edges`; the detail
// shapes carry the *other* end of each connection as a ref the UI links to, so
// the owner follows Claim → Protocol → Concept by navigation, not a graph view.

export type ConceptRef = { id: number; name: string };
export type ClaimRef = { id: number; text: string };
export type ProtocolRef = { id: number; action: string };
export type Source = { video_id: string; title: string };

export type BokClaim = {
  id: number;
  text: string;
  type: string;
  locator_seconds: number;
  deep_link: string;
  protected: boolean;
  source: Source;
  concepts: ConceptRef[];
  supports: ProtocolRef[]; // Protocols this Claim justifies (detail only)
};

export type BokProtocol = {
  id: number;
  action: string;
  dose: string | null;
  timing: string | null;
  frequency: string | null;
  duration: string | null;
  locator_seconds: number;
  deep_link: string;
  protected: boolean;
  source: Source;
  concepts: ConceptRef[];
  justified_by: ClaimRef[]; // Claims that support it (detail only)
};

export type BokConcept = {
  id: number;
  name: string;
  kind: string | null;
  reference_count: number;
  claims: ClaimRef[]; // what references it (detail only)
  protocols: ProtocolRef[];
};

export type ClaimEdit = { text: string; type: string; locator_seconds: number };
export type ProtocolEdit = {
  action: string;
  dose: string | null;
  timing: string | null;
  frequency: string | null;
  duration: string | null;
  locator_seconds: number;
};

function patch<T>(path: string, body: unknown): Promise<T> {
  return json(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function qs(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  if (entries.length === 0) return "";
  return "?" + entries.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&");
}

export function listClaims(filter: {
  conceptId?: number;
  type?: string;
}): Promise<{ claims: BokClaim[] }> {
  return json(`/api/claims${qs({ concept_id: filter.conceptId, type: filter.type })}`);
}

export function getClaim(id: number): Promise<BokClaim> {
  return json(`/api/claims/${id}`);
}

export function editClaim(id: number, body: ClaimEdit) {
  return patch(`/api/claims/${id}`, body);
}

export function deleteClaim(id: number) {
  return json(`/api/claims/${id}`, { method: "DELETE" });
}

export function listProtocols(filter: {
  conceptId?: number;
}): Promise<{ protocols: BokProtocol[] }> {
  return json(`/api/protocols${qs({ concept_id: filter.conceptId })}`);
}

export function getProtocol(id: number): Promise<BokProtocol> {
  return json(`/api/protocols/${id}`);
}

export function editProtocol(id: number, body: ProtocolEdit) {
  return patch(`/api/protocols/${id}`, body);
}

export function deleteProtocol(id: number) {
  return json(`/api/protocols/${id}`, { method: "DELETE" });
}

export function listConcepts(filter: {
  kind?: string;
}): Promise<{ concepts: BokConcept[] }> {
  return json(`/api/concepts${qs({ kind: filter.kind })}`);
}

export function getConcept(id: number): Promise<BokConcept> {
  return json(`/api/concepts/${id}`);
}

// -- The personal layer (issue #16) -----------------------------------------
// The owner-specific layer (CONTEXT.md "Personal Layer"): Goals, Markers,
// Decisions, recorded through guided forms and linked to the evidence layer by
// Concept overlap. A Marker reading is append-only (the API rejects an overwrite)
// and "out of range" is derived server-side from the stored reference range. A
// Decision carries its *own* actual parameters, distinct from the Protocol it
// implements; the suggester returns Protocols/Claims/Goals it overlaps with by
// Concept, which the owner confirms one at a time.

export type GoalRef = { id: number; title: string };
export type DecisionRef = { id: number; action: string };
export type MarkerRef = {
  id: number;
  concept: string;
  value: number;
  unit: string;
  measured_at: string;
};

export type Goal = {
  id: number;
  title: string;
  detail: string | null;
  concepts: ConceptRef[];
  served_by: DecisionRef[]; // Decisions serving it; empty ⇒ an unmet Goal
};

export type MarkerReading = {
  id: number;
  concept: ConceptRef;
  value: number;
  unit: string;
  reference_low: number | null;
  reference_high: number | null;
  measured_at: string;
  out_of_range: boolean; // derived from the reference range, never stored
};

export type MarkerSeries = {
  concept: ConceptRef;
  unit: string;
  reading_count: number;
  latest: MarkerReading;
  out_of_range: boolean;
};

export type Decision = {
  id: number;
  action: string;
  dose: string | null;
  timing: string | null;
  frequency: string | null;
  duration: string | null;
  started_at: string;
  ended_at: string | null;
  note: string | null;
  concepts: ConceptRef[];
  implements: ProtocolRef[]; // Protocol(s) it implements (detail only)
  serves: GoalRef[]; // Goal(s) it serves (detail only)
  motivated_by: MarkerRef[]; // Marker reading(s) behind it (detail only)
  supported_by: ClaimRef[]; // Claim(s) that support it (detail only)
};

export type SuggestedLink = {
  target_type: "protocol" | "claim" | "goal";
  target_id: number;
  label: string;
  shared_concepts: string[];
};

export type NewGoal = { title: string; detail: string | null; concepts: string[] };
export type NewMarker = {
  concept: string;
  value: number;
  unit: string;
  reference_low: number | null;
  reference_high: number | null;
  measured_at: string;
};
export type NewDecision = {
  action: string;
  dose: string | null;
  timing: string | null;
  frequency: string | null;
  duration: string | null;
  started_at: string;
  ended_at: string | null;
  note: string | null;
  concepts: string[];
  implements_protocol_id: number | null;
};

function post<T>(path: string, body: unknown): Promise<T> {
  return json(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function listGoals(): Promise<{ goals: Goal[] }> {
  return json("/api/goals");
}

export function getGoal(id: number): Promise<Goal> {
  return json(`/api/goals/${id}`);
}

export function createGoal(body: NewGoal): Promise<{ id: number }> {
  return post("/api/goals", body);
}

export function deleteGoal(id: number) {
  return json(`/api/goals/${id}`, { method: "DELETE" });
}

export function listMarkers(): Promise<{ markers: MarkerSeries[] }> {
  return json("/api/markers");
}

export function getMarkerHistory(
  conceptId: number,
): Promise<{ concept_id: number; readings: MarkerReading[] }> {
  return json(`/api/markers/${conceptId}`);
}

export function listMarkerReadings(): Promise<{ readings: MarkerReading[] }> {
  return json("/api/marker-readings");
}

export function createMarker(body: NewMarker): Promise<{ id: number }> {
  return post("/api/markers", body);
}

export function listDecisions(): Promise<{ decisions: Decision[] }> {
  return json("/api/decisions");
}

export function getDecision(id: number): Promise<Decision> {
  return json(`/api/decisions/${id}`);
}

export function createDecision(body: NewDecision): Promise<{ id: number }> {
  return post("/api/decisions", body);
}

export function deleteDecision(id: number) {
  return json(`/api/decisions/${id}`, { method: "DELETE" });
}

export function getDecisionSuggestions(
  id: number,
): Promise<{ suggestions: SuggestedLink[] }> {
  return json(`/api/decisions/${id}/suggestions`);
}

export function linkDecision(
  id: number,
  link: { target_type: string; target_id: number },
) {
  return post(`/api/decisions/${id}/links`, link);
}

export function unlinkDecision(id: number, targetType: string, targetId: number) {
  return json(
    `/api/decisions/${id}/links?target_type=${targetType}&target_id=${targetId}`,
    { method: "DELETE" },
  );
}
