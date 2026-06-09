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
    throw new Error(`${init?.method ?? "GET"} ${path} failed: ${res.status}`);
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
