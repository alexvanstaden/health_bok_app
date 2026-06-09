# Web App stack: Python API backend + Next.js frontend, containerized, behind Tailscale

ADR-0007 established a primary web interface but pinned no stack and left its name provisional.
This records the stack and retires the name.

## Decision

- **The primary interface is renamed "Console" → "Web App"** (ADR-0007's term is retired). A
  Python **CLI** may exist for ops/admin convenience but is *not* a committed product surface.
- **Frontend: Next.js (React).** A dedicated web frontend, not server-rendered HTML.
- **Backend: a Python HTTP API** that reuses the existing `health_bok` domain (`repository.py`,
  models) so the Web App and the daily pipeline share one codebase and the one Postgres
  (ADR-0003, ADR-0007).
- **Deployment: Docker containers** on the VPS — Postgres, the Python API, the Next.js app, and a
  background **worker**; the existing daily pipeline runs as a scheduled container rather than host
  cron/systemd.
- **Background work: a Postgres-backed `jobs` table drained by the worker** (no Redis/Celery), so
  "single Postgres" (ADR-0003) stays literally true and approval can enqueue long work
  (transcribe → extract) without blocking a request.
- **Access: behind Tailscale only**, never exposed to the public internet. Single-user, so **the
  tailnet *is* the auth boundary — there is no login screen in v1.** An app-password → session can
  be added later if the app is ever exposed beyond the tailnet, or if device-level protection is
  wanted; it is deliberately out of v1 to match the system's simplicity bias.

## Considered Options

- **Python monolith, server-rendered (FastAPI + HTMX), one island of JS for graph viz** —
  simpler, one language, shares the domain directly, one deploy artifact. Rejected: the owner
  wants the web UI to be *the* product with a richer interaction ceiling than server-rendered
  HTML comfortably gives, and is willing to pay for it.

## Consequences

- **Trade-off accepted:** two languages, an HTTP API seam, and container orchestration, in
  exchange for a first-class frontend. The Python ports/repository pattern now also backs an API
  layer.
- **Amends ADR-0007:** interactive **graph visualization is deferred out of v1 scope**;
  **natural-language query** of the Body of Knowledge becomes the primary exploration mechanism in
  its place (its design is TBD).
- **No new datastore.** Containerization does not change ADR-0003; Supabase-portability
  (`pg_dump` away) is preserved.
- **Email (Resend) unchanged** and still inessential (ADR-0007).
