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
  creator: string;
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

// -- Queue filtering: processing-status (issue #75) -------------------------
// The shared filter plumbing both review queues follow. A Candidate in either
// queue sits in one of these lifecycle states; the toolbar lets the owner narrow
// to a subset, server-side via repeatable `status` query params. Admitted/rejected
// ones have left the queue, so they are deliberately not selectable.
export type ProcessingStatus = "candidate" | "approved" | "processing" | "failed";
export const PROCESSING_STATUSES: ProcessingStatus[] = [
  "candidate",
  "approved",
  "processing",
  "failed",
];

// The full set of queue filter dimensions both review queues share (issues #75, #76),
// AND-composed server-side. Every field is optional; an empty `QueueFilters` returns
// the full queue. `creators` holds channel_ids (from `/api/creators`); the date bounds
// are `YYYY-MM-DD` strings, inclusive on both ends; `search` is a free-text term
// matched against title + creator name + description (the Summary on the Review queue).
export type QueueFilters = {
  statuses?: ProcessingStatus[];
  creators?: string[];
  publishedFrom?: string;
  publishedTo?: string;
  search?: string;
};

// Whether any filter dimension is set — drives the queues' "no matches" vs. "empty
// queue" empty state, so the owner can tell a filtered-to-nothing queue from a
// genuinely empty one.
export function hasActiveFilters(filters: QueueFilters = {}): boolean {
  return (
    (filters.statuses?.length ?? 0) > 0 ||
    (filters.creators?.length ?? 0) > 0 ||
    !!filters.publishedFrom ||
    !!filters.publishedTo ||
    !!filters.search?.trim()
  );
}

// Encode the filters as query params: repeatable `status=`/`creator=` (FastAPI reads
// each as a list), plus single `published_from`/`published_to`/`q`. Absent or empty
// dimensions contribute nothing, so the unfiltered queue is a bare path.
function queueParams(filters: QueueFilters = {}): string {
  const parts: string[] = [];
  for (const s of filters.statuses ?? []) parts.push(`status=${encodeURIComponent(s)}`);
  for (const c of filters.creators ?? []) parts.push(`creator=${encodeURIComponent(c)}`);
  if (filters.publishedFrom) parts.push(`published_from=${encodeURIComponent(filters.publishedFrom)}`);
  if (filters.publishedTo) parts.push(`published_to=${encodeURIComponent(filters.publishedTo)}`);
  if (filters.search?.trim()) parts.push(`q=${encodeURIComponent(filters.search.trim())}`);
  return parts.join("&");
}

export function listCandidates(
  filters: QueueFilters = {},
): Promise<{ candidates: Candidate[] }> {
  const params = queueParams(filters);
  return json(`/api/candidates${params ? `?${params}` : ""}`);
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

// -- Logs: the record of admitted/failed video Sources (issue #33) ----------
// A read-only list of every video the pipeline carried to a terminal admission,
// newest-first. `bok_state` is `admitted` (reached the Body of Knowledge) or
// `failed` (extraction errored); videos still in flight or never approved are not
// listed. It has no actions. Each row links to that video's existing Claims page.

export type ProcessedVideo = {
  video_id: string;
  title: string;
  creator: string;
  added_at: string;
  summary: string | null; // null when admitted without a Summary, e.g. backfill (issue #79)
  bok_state: "admitted" | "failed";
};

export function listVideos(): Promise<{ videos: ProcessedVideo[] }> {
  return json("/api/videos");
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

export type BackfillOrder = "newest" | "oldest";

export function listBackfillCandidates(
  order: BackfillOrder = "newest",
  filters: QueueFilters = {},
): Promise<{ candidates: BackfillCandidate[] }> {
  const params = queueParams(filters);
  return json(`/api/backfill?order=${order}${params ? `&${params}` : ""}`);
}

// Lazily fetch one Candidate's real description + accurate publish date (issue #31):
// one per-video extraction, persisted, so the queue shows them in place on demand.
export function fetchBackfillDetails(videoId: string): Promise<BackfillCandidate> {
  return json(`/api/backfill/${videoId}/fetch-details`, { method: "POST" });
}

export function rejectBackfillCandidates(videoIds: string[]): Promise<{ rejected: number }> {
  return json("/api/backfill/reject", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_ids: videoIds }),
  });
}

// Bulk-approve the selected backfill Candidates in one gesture (issue #73). Returns
// how many were *newly* approved; already in-flight ones are skipped server-side, so
// re-sending is safe. Each fresh approval runs the same pipeline as a daily Candidate.
export function approveBackfillCandidates(videoIds: string[]): Promise<{ approved: number }> {
  return json("/api/backfill/approve", {
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

// A referenced Concept grouped with the admitted Claims that also reference it —
// the per-Concept evidence on a Protocol's detail (issue #85). Claims already in
// `justified_by` are omitted so nothing reads as double-counted evidence.
export type ConceptClaims = { id: number; name: string; claims: ClaimRef[] };

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
  concept_claims: ConceptClaims[]; // per referenced Concept, its related Claims (detail only)
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
  goalId?: number;
}): Promise<{ protocols: BokProtocol[] }> {
  return json(
    `/api/protocols${qs({ concept_id: filter.conceptId, goal_id: filter.goalId })}`,
  );
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

// -- The Concept neighbourhood view (issue #51, ADR-0013) -------------------
// The lateral, Strength-ranked map of what a Concept connects to. Each relation
// is a directed, signed-predicate link (src → predicate → dst), ranked by
// evidence Strength (distinct creators × trust-tier × recency), flagged when the
// pair is contested, and carrying the evidencing Claims as Citations — the *same*
// shape NL Query returns — each clickable through to its Source + locator. Query
// stays the primary exploration surface (ADR-0009/0011); this is the visual map.

export type RelationCitation = {
  claim_id: number;
  text: string;
  type: string;
  deep_link: string; // back to the moment in the Source (watch?v=ID&t=NNNs)
  source_title: string;
};

export type NeighbourRelation = {
  relation_id: number;
  src: ConceptRef;
  predicate: string;
  dst: ConceptRef;
  strength: number;
  creator_count: number;
  contested: boolean;
  via: ConceptRef | null; // the descendant a rolled-up relation lives on
  evidence_claim_ids: number[];
  evidence: RelationCitation[];
};

export type Neighbourhood = {
  concept: ConceptRef;
  sub_concepts: ConceptRef[];
  relations: NeighbourRelation[];
};

export function getConceptNeighbourhood(id: number): Promise<Neighbourhood> {
  return json(`/api/concepts/${id}/neighbourhood`);
}

// -- The broader-of review queue (ADR-0014) ---------------------------------
// The two-tier auto path confirms confident parents outright and leaves looser
// ones *proposed* — these are the ones awaiting a one-click confirm/reject, so a
// wrong guess never silently organizes the taxonomy.

export type BroaderOfProposal = {
  narrower_id: number;
  narrower_name: string;
  broader_id: number;
  broader_name: string;
  // Cosine distance between the two Concepts' embeddings — the score the auto-confirm
  // gate used. Lower is a closer, more confident match. `null` if either lacks an
  // embedding.
  distance: number | null;
};

export function getBroaderOfProposals(): Promise<{ proposals: BroaderOfProposal[] }> {
  return json(`/api/broader-of/proposals`);
}

export function confirmBroaderOf(narrowerId: number, broaderId: number) {
  return json(`/api/concepts/${narrowerId}/broader-of/${broaderId}/confirm`, {
    method: "POST",
  });
}

export function rejectBroaderOf(narrowerId: number, broaderId: number) {
  return json(`/api/concepts/${narrowerId}/broader-of/${broaderId}`, {
    method: "DELETE",
  });
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

// A Goal's Concepts are editable after creation (issue #37): attach by picking from
// the catalogue or typing a new term (normalized server-side, reused or minted), and
// detach an attached one. Each persists as a `references` edge.
export function attachGoalConcept(id: number, name: string) {
  return post(`/api/goals/${id}/concepts`, { name });
}

export function detachGoalConcept(id: number, conceptId: number) {
  return json(`/api/goals/${id}/concepts/${conceptId}`, { method: "DELETE" });
}

// An existing Concept a Goal likely concerns, inferred from its title + detail over
// pgvector (issue #38). Conservative: every suggestion already exists in the
// catalogue (none minted) and one already attached is never suggested. Confirm one
// in a single click through attachGoalConcept (issue #37).
export type ConceptSuggestion = { concept_id: number; name: string; distance: number };

export function goalConceptSuggestions(
  id: number,
): Promise<{ suggestions: ConceptSuggestion[] }> {
  return json(`/api/goals/${id}/concept-suggestions`);
}

// A NEW Concept to mint for a Goal, proposed by an LLM from its title + detail
// (issue #39). The companion to goalConceptSuggestions: these terms resolve to no
// existing Concept (checked with the same conservative ConceptNormalizer logic), so
// confirming one mints the Concept and attaches it — through the same
// attachGoalConcept (issue #37). Nothing is minted until the owner confirms; an LLM
// failure yields an empty list, leaving the existing-Concept suggestions intact.
export type NewConceptSuggestion = { name: string };

export function goalNewConceptSuggestions(
  id: number,
): Promise<{ suggestions: NewConceptSuggestion[] }> {
  return json(`/api/goals/${id}/new-concept-suggestions`);
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

// -- Natural-language query: grounded & cited (issue #17) -------------------
// The primary way the owner *explores* the Body of Knowledge now that a visual
// graph is out of v1 scope (ADR-0009, ADR-0011). A free-text question is answered
// STRICTLY from the owner's own library — Claims, Protocols, and personal layer —
// never the model's general knowledge. Every answer cites the specific Claims it
// rests on, each clickable through to its Source and locator; when nothing covers
// the question the assistant abstains rather than confabulating. Grounding and
// cite-or-abstain are enforced server-side.

export type Citation = {
  claim_id: number;
  text: string;
  type: string;
  deep_link: string; // back to the moment in the Source (watch?v=ID&t=NNNs)
  source_title: string;
};

export type QueryAnswer = {
  question: string;
  answer: string;
  abstained: boolean; // true ⇒ "nothing in your library covers this"
  citations: Citation[]; // empty when abstained; ≥1 otherwise
};

export function askQuestion(question: string): Promise<QueryAnswer> {
  return post("/api/query", { question });
}

// -- The Impact engine: inbox & lifecycle (issue #18) -----------------------
// Change detection's read/act surface. New evidence (a just-admitted
// Claim/Protocol) and new choices (a recorded Decision/Goal) raise stance-typed
// Impacts against the owner's anchors; the inbox is filterable by stance and
// anchor, and each Impact walks new → reviewed → actioned | dismissed so it never
// re-nags. Actioning records the Decision the owner revised or created in response;
// a burst can be bulk-dismissed. Detection, dedup, and the lifecycle live server-side.

export type Stance = "reinforces" | "contradicts" | "refines" | "opportunity";
export type ImpactState = "new" | "reviewed" | "actioned" | "dismissed";

export type ImpactEnd = { type: string; id: number; label: string };

export type Impact = {
  id: number;
  source: ImpactEnd; // the Claim/Protocol that triggered it
  anchor: ImpactEnd; // the Decision/Goal/Marker it bears on
  stance: Stance;
  state: ImpactState;
  detail: string | null;
  actioned_decision_id: number | null;
  created_at: string;
};

export function listImpacts(filter: {
  stance?: string;
  anchorType?: string;
  anchorId?: number;
  state?: string;
}): Promise<{ impacts: Impact[] }> {
  return json(
    `/api/impacts${qs({
      stance: filter.stance,
      anchor_type: filter.anchorType,
      anchor_id: filter.anchorId,
      state: filter.state,
    })}`,
  );
}

export function reviewImpact(id: number) {
  return json(`/api/impacts/${id}/review`, { method: "POST" });
}

export function dismissImpact(id: number) {
  return json(`/api/impacts/${id}/dismiss`, { method: "POST" });
}

export function actionImpact(id: number, decisionId: number) {
  return post(`/api/impacts/${id}/action`, { decision_id: decisionId });
}

export function bulkDismissImpacts(impactIds: number[]): Promise<{ dismissed: number }> {
  return post("/api/impacts/dismiss", { impact_ids: impactIds });
}
