# Health & Longevity Knowledge System

A personal system that monitors health & longevity content creators, archives and
summarizes their material, and (later) links it into a personalized knowledge graph.
See [`CONTEXT.md`](CONTEXT.md) for the domain language and [`docs/adr/`](docs/adr/)
for the architectural decisions.

## Status

**Slice 1 — walking skeleton (issue #2).** The thinnest end-to-end path through
every layer. For one known video the pipeline:

1. fetches the video's YouTube captions as its **Transcript** (with timestamps),
2. archives that Transcript **immutably** in Postgres with full provenance,
3. generates a prose **Summary** via the Claude API, and
4. sends a one-item **Digest** email (linking to the source video) via Resend.

**Slice 2 — Creator management (issue #3).** The owner maintains a watch list of
**Creators**. A Creator is added by @handle or channel URL; the reference is
resolved to its underlying YouTube `channel_id` **exactly once** at add-time and
stored with the Creator's name as a stable identity. Re-adding the same Creator
(even via a different handle/URL that resolves to the same channel) never
duplicates it. The daily job reads this list. See [Manage Creators](#manage-creators).

Everything else — RSS discovery against the watch list, the Whisper fallback,
map-reduce summarization of long videos, backfill Candidates, and all of Part 2
(the knowledge graph) — is deferred to later slices.

## Architecture

A single self-hosted **Postgres** is the only source of truth (ADR-0003). The
raw Transcript is the permanent record; the Summary is disposable and
re-derivable (ADR-0001). No graph/entity extraction runs here.

Three **ports** isolate every external boundary so the job can be tested with
fakes while Postgres stays real:

| Port            | Real adapter (`health_bok/adapters/`) | Service             |
| --------------- | ------------------------------------- | ------------------- |
| `ContentSource` | `youtube.py`                          | YouTube (yt-dlp + captions); also resolves an @handle/URL to a `channel_id` |
| `Summarizer`    | `claude.py`                           | Claude API          |
| `DigestSender`  | `resend.py`                           | Resend              |

```
health_bok/
├── config.py        env-var configuration (no secrets in code)
├── models.py        domain types (CreatorIdentity, Transcript, Provenance, Summary, Digest)
├── ports.py         the three port protocols
├── db.py            Postgres connection + schema bootstrap
├── schema.sql       creators / videos / transcripts / summaries / processing_state
├── repository.py    persistence (creators, archive, summarize, mark processed/sent)
├── creators.py      Creator-management service (add / remove the watch list)
├── job.py           the orchestrator (run_job)
├── main.py          CLI: `run` (daily job) + `creators add|remove|list`
└── adapters/        youtube · claude · resend
```

## Setup

Requires Python 3.10+ and (for the test suite) a running Docker daemon.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in the values
```

All secrets come from the environment — never hard-coded. See `.env.example`:

| Variable                   | Purpose                                            |
| -------------------------- | -------------------------------------------------- |
| `DATABASE_URL`             | Postgres connection (the single source of truth)   |
| `ANTHROPIC_API_KEY`        | Claude — the Summarizer                            |
| `CLAUDE_MODEL`             | Summarization model (default `claude-sonnet-4-6`)  |
| `RESEND_API_KEY`           | Resend — the DigestSender                          |
| `DIGEST_FROM`              | Verified Resend "from" address                      |
| `DIGEST_RECIPIENT`         | Where the Digest is delivered                       |
| `OPENAI_API_KEY`           | Whisper fallback (read now, used in a later slice)  |
| `WALKING_SKELETON_VIDEO_ID`| The one video slice 1 processes                     |

## Run the pipeline

```bash
source .venv/bin/activate
health-bok            # or: health-bok run, or: python -m health_bok
```

This applies the schema if needed, then runs the job for `WALKING_SKELETON_VIDEO_ID`.
It is **idempotent**: re-running reprocesses nothing already archived+summarized
and sends no second email. A failed Digest send can be retried without
re-summarizing.

## Manage Creators

Maintain the watch list of Creators the system follows. These commands need only
`DATABASE_URL` — not the Digest or Summarizer secrets.

```bash
health-bok creators add @hubermanlab                          # by @handle
health-bok creators add https://www.youtube.com/@PeterAttiaMD # or by channel URL
health-bok creators list                                      # channel_id <TAB> name
health-bok creators remove UC2D2CMWXMOVWx7giW1n3LIg           # by channel_id (from list)
```

On `add`, the @handle/URL is resolved to its YouTube `channel_id` **once** and
stored with the Creator's name; the daily job thereafter keys off that stable
`channel_id` and never re-resolves. Adding the same Creator again — even via a
different handle or URL — refreshes the name but creates no duplicate. Removal is
by `channel_id` (shown by `list`), so it stays reliable even if a channel later
changes its handle.

## Tests

The primary test drives the whole job end-to-end with the three ports faked and a
**real ephemeral Postgres** (spun up via `testcontainers`), asserting on the
persisted records and the captured Digest. It needs Docker running.

```bash
source .venv/bin/activate
pytest
```
