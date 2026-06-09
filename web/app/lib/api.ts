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
