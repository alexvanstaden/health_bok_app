# Health & Longevity Knowledge System

A personal system that monitors health & longevity content creators, archives and
summarizes their material, and links it into a personalized knowledge graph connecting
evidence to the owner's health decisions, markers, and goals. The owner works through a
self-hosted **Web App** (ADR-0007, ADR-0009); the daily email **Digest** is only a
notification that links back into it. See [`CONTEXT.md`](CONTEXT.md) for the domain
language and [`docs/adr/`](docs/adr/) for the architectural decisions.

The system is built in two parts:

- **Part 1 — the daily pipeline** (PRD issue #1) — **built today.** Watches Creators,
  detects new uploads, archives immutable Transcripts in Postgres, summarizes them, and
  emails a Digest. **This README documents Part 1: what it does and how to run it.**
- **Part 2 — the Web App & knowledge graph** (PRD issue #12; ADRs 0007–0011) — *in
  progress.* The self-hosted Web App is the primary interface: the owner approves
  Candidates, extraction draws Claims and Protocols into the Body of Knowledge, records
  the personal layer (Goals, Markers, Decisions), and queries it all in grounded, cited
  natural language. Tracked in slices #13–18 — **slice 8 (issue #13), "Approve → Extract
  → See Claims", and slice 9 (issue #14), "Browse & edit the Body of Knowledge", are now
  built** (see below); the rest follow.

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

**Slice 3 — daily detection across Creators (issue #4).** The real daily job.
For **every** watched Creator it fetches the YouTube RSS feed and detects new
uploads by diffing the feed's video IDs against the already-processed set. Only
the new videos run through the spine (Transcript → archive → Summary); all of a
run's new Summaries are bundled into **one Digest** and emailed — but only when
there is new content. It is idempotent (a video is marked processed only after
its Transcript *and* Summary are persisted, so a re-run reprocesses nothing), a
failed Digest send retries without re-summarizing ("sent" is tracked separately
from "processed"), and one Creator's error never aborts the rest of the run. The
job is wired to run daily at ~6am — see [Schedule the daily job](#schedule-the-daily-job).

**Slice 4 — Whisper fallback for caption-less videos (issue #5).** Free YouTube
captions are still preferred, but when a *new* video has none, the daily job
downloads its audio with `yt-dlp` and transcribes it via the OpenAI **Whisper**
API, then continues through the normal spine — so no video is skipped for lack
of captions. Which transcript source was used (`captions` vs `whisper`) is
recorded on the video's provenance. Whisper runs **only** on the daily path,
never for backfill.

**Slice 5 — map-reduce summarization for long videos (issue #6).** Short
Transcripts still summarize in a single Claude call. A Transcript longer than a
configurable threshold (`SUMMARY_MAX_CHARS`) is split on segment boundaries into
sections of at most `SUMMARY_CHUNK_CHARS`, each section is summarized, and those
section Summaries are reduced — through the same Summarizer — into one final
**Summary**. Map-reduce is transparent to the job: the final Summary is persisted
and digested like any other, so a multi-hour podcast never breaks the pipeline.

**Slice 6 — backfill Candidate population (issue #7).** Adding a Creator now also
seeds its back-catalogue. The Creator's past uploads are listed (via `yt-dlp`'s
flat playlist) and each one published within a configurable recency window
(`BACKFILL_CUTOFF_DAYS`, default ~2 years) is stored as a **Candidate** — *metadata
only*: title, description, publish date, and URL. No Transcript is fetched, no
Summary is generated, and **Whisper is never called** for backfill. A backfill
Candidate stays metadata-only until the owner approves it into the Body of
Knowledge (ADR-0004); listing is idempotent, so re-adding a Creator only tops up
newly-published Candidates. Reviewing and approving Candidates — and the
processing that approval triggers — is Part 2 (below).

**Slice 8 — Approve → Extract → See Claims (issue #13).** The first Part-2 vertical, end
to end through every new layer. The whole stack now runs under **docker-compose**
(ADR-0009): Postgres, a Python **HTTP API** over the existing `health_bok` domain, the
**Next.js Web App**, a background **worker**, and the daily pipeline as a scheduled
container. The Web App loads over Tailscale with **no login** — the tailnet is the auth
boundary. In it the owner sees each daily **Candidate** with its Summary and can:

- **Approve** — enqueues a job in a Postgres-backed `jobs` table and returns immediately;
  the worker drains it, walking the Candidate `approved → processing → admitted`.
- The worker **extracts** Claims and Protocols precision-first (scope qualifiers
  preserved; grounded-or-dropped with locator deep-links; Protocols only when structured —
  action + dose/timing/frequency/duration, else the assertion stays a Claim), **normalizes
  Concepts** (embed each mention → nearest-Concept match via pgvector → merge when close,
  else mint a new Concept), and **auto-admits** — there is no second gate (ADR-0010).
- **See Claims** — view a video's admitted Claims and Protocols, each with its Concepts
  and a locator deep-link (`watch?v=ID&t=NNNs`) back to the moment it was asserted.
- **Reject** a Candidate (removed from the queue, never admitted) or **Retry** one whose
  extraction **failed** (the failure is visible and retryable).

The daily **Digest** is demoted to a notification that deep-links into the Web App review
queue; set `DIGEST_ENABLED=false` and the system stays fully usable with email off
(ADR-0007).

**Slice 9 — Browse & edit the Body of Knowledge (issue #14).** The admitted evidence
layer is now browsable and editable in the Web App — no visual graph (ADR-0009), the
connections are followed by navigation. The owner can:

- **Browse** filterable lists of **Claims** (by sub-kind and by referenced Concept),
  **Protocols** (by action and Concept), and **Concepts** (by name) — reachable from the
  top nav.
- **Follow connections** by clicking: a Claim links to the Concepts it references and the
  Protocols it supports; a Protocol links to the Claims that justify it and its Concepts;
  a Concept links to everything that references it — all by traversing the `edges`
  (ADR-0008), with each Claim/Protocol keeping its locator deep-link back into the source.
- **Edit or delete in place** (ADR-0010): every admitted Claim and Protocol can be
  corrected or removed directly while browsing — curation continues opportunistically
  rather than via a standing review queue. A delete also clears the edges that hung off
  the entity, so none dangle.
- An **owner edit is a protected version** (ADR-0005/0010): editing flags the Claim or
  Protocol `protected`, the hook a later re-extraction supersede pass reads so it never
  silently clobbers a hand-correction. (Re-extraction itself is a later slice.)

The personal layer (Goals, Markers, Decisions) and grounded natural-language query are
the remaining Part-2 slices (#15–18).

## Architecture

A single self-hosted **Postgres** is the only source of truth (ADR-0003). The
raw Transcript is the permanent record; the Summary is disposable and
re-derivable (ADR-0001). Part 2 layers the knowledge graph onto this same
Postgres (ADR-0008): typed entity tables (`claims`, `protocols`, `concepts`), one
polymorphic `edges` table (integrity enforced by trigger, idempotent on a unique
key), an append-only `embeddings` table (pgvector + HNSW), a `jobs` queue, and the
daily-Candidate `admissions` lifecycle. Extraction fills them (ADR-0010).

**Ports** isolate every external boundary so the job and worker can be tested with
fakes while Postgres stays real. Part 1's four, plus Part 2's two:

| Port            | Real adapter (`health_bok/adapters/`) | Service             |
| --------------- | ------------------------------------- | ------------------- |
| `ContentSource` | `youtube.py`                          | YouTube — resolves an @handle/URL to a `channel_id`, lists the back-catalogue for backfill Candidates, discovers new uploads via RSS, fetches captions, downloads audio for the Whisper fallback |
| `Transcriber`   | `whisper.py`                          | OpenAI Whisper — transcribes a caption-less video's audio (daily path only) |
| `Summarizer`    | `claude.py` (single pass), wrapped by `summarizer.py` (map-reduce for long Transcripts) | Claude API          |
| `DigestSender`  | `resend.py`                           | Resend              |
| `Extractor`     | `extractor.py`                        | Claude API — Transcript → Claims + Protocols + Concept mentions (precision-first, ADR-0010) |
| `Embedder`      | `embedder.py`                         | OpenAI `text-embedding-3-small` — 1536-d vectors for Concept normalization (ADR-0008) |

```
health_bok/
├── config.py        env-var configuration (no secrets in code)
├── models.py        domain types (Transcript, Provenance, Summary, Digest; Extraction/Claim/Protocol)
├── ports.py         the six port protocols
├── db.py            Postgres connection + schema bootstrap
├── schema.sql       Part 1 (creators/candidates/videos/transcripts/summaries/processing_state)
│                    + Part 2 (concepts/claims/protocols/edges/embeddings/jobs/admissions)
├── repository.py    persistence for both parts (one Repository over the one Postgres)
├── creators.py      Creator-management service (add / remove the watch list)
├── backfill.py      backfill Candidate population: list back-catalogue → metadata-only Candidates
├── summarizer.py    MapReduceSummarizer: chunk + reduce long Transcripts
├── job.py           the daily orchestrator (run_job): RSS detect → spine → one Digest
├── review.py        owner-driven Candidate transitions: approve · reject · retry (Part 2)
├── admit.py         extract → ground/structure → normalize Concepts → auto-admit (Part 2)
├── concepts.py      ConceptNormalizer: embed → nearest via pgvector → merge/new (Part 2)
├── curation.py      in-place edit/delete of admitted Claims & Protocols; edit-protection (Part 2)
├── worker.py        drains the jobs queue (FOR UPDATE SKIP LOCKED); lifecycle (Part 2)
├── api.py           FastAPI HTTP API the Web App calls: review queue + BoK browse/edit (Part 2)
├── main.py          CLI: `run` (daily job) · `worker` (drain queue) · `creators …`
└── adapters/        youtube · whisper · claude · resend · extractor · embedder

web/                 the Next.js Web App — review queue + BoK browser (Claims/Protocols/Concepts) — Part 2
Dockerfile           Python image: API · worker · scheduled pipeline (one image, three commands)
docker-compose.yml   db (pgvector) · api · worker · web · pipeline — the Part-2 stack
deploy/              cron + systemd units (the Part-1 host-scheduling alternative)
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
| `SUMMARY_MAX_CHARS`        | Map-reduce above this Transcript length (default `48000`) |
| `SUMMARY_CHUNK_CHARS`      | Section size when map-reducing (default `16000`)   |
| `BACKFILL_CUTOFF_DAYS`     | Backfill window when a Creator is added (default `730`, ~2 years) |
| `DIGEST_ENABLED`           | Send the Digest email at all (default `true`; `false` → no Resend secrets needed) |
| `RESEND_API_KEY`           | Resend — the DigestSender (only when `DIGEST_ENABLED`) |
| `DIGEST_FROM`              | Verified Resend "from" address (only when `DIGEST_ENABLED`) |
| `DIGEST_RECIPIENT`         | Where the Digest is delivered (only when `DIGEST_ENABLED`) |
| `OPENAI_API_KEY`           | OpenAI — Whisper transcription **and** Concept embeddings |
| `EXTRACTION_MODEL`         | Claude model for extraction (Part 2; default `claude-sonnet-4-6`) |
| `EMBEDDING_MODEL`          | Embedding model for Concept normalization (Part 2; default `text-embedding-3-small`) |
| `CONCEPT_MERGE_DISTANCE`   | Cosine-distance merge threshold for Concepts (Part 2; default `0.15`) |
| `WEBAPP_BASE_URL`          | Web App base URL for Digest deep-links (Part 2; optional) |

## Run the pipeline

```bash
source .venv/bin/activate
health-bok            # or: health-bok run, or: python -m health_bok
```

This applies the schema if needed, then runs the daily job across **every**
watched Creator (see [Manage Creators](#manage-creators) to populate the list):

1. fetch each Creator's YouTube RSS feed and find new uploads by diffing the
   feed's video IDs against the already-processed set;
2. run only the new videos through the spine (Transcript → archive → Summary),
   acquiring the Transcript from free YouTube captions when present and falling
   back to **Whisper** (audio download → transcription) when the video has none,
   and summarizing long Transcripts via map-reduce (chunk → summarize → reduce);
3. bundle the run's new Summaries into **one Digest** and email it — sent only
   when there is new content.

It is **idempotent**: a video is marked processed only after its Transcript *and*
Summary are persisted, so re-running reprocesses nothing already done and sends
no second email. A failed Digest send is retried on the next run without
re-summarizing ("sent" is tracked separately from "processed"). One Creator's
error (an unreachable feed, a missing transcript) is isolated and never aborts
the rest of the run.

## Run the Web App (Part 2)

The whole Part-2 stack runs under docker-compose (ADR-0009): Postgres (pgvector),
the HTTP API, the Next.js Web App, the worker, and the daily pipeline as a
scheduled container.

```bash
cp .env.example .env        # fill in the secrets (or set DIGEST_ENABLED=false)
docker compose up --build   # db + api + web + worker + pipeline
```

Then open the Web App at **http://localhost:3000** (in production, bind the
published ports to your **Tailscale** address — the tailnet is the auth boundary,
so there is no login screen). The flow:

1. The home page is the **review queue**: each daily Candidate with its Summary.
2. **Approve** a Candidate — the API enqueues a job and returns immediately; the
   **worker** extracts Claims/Protocols, normalizes Concepts, and auto-admits,
   and you watch the badge walk `approved → processing → admitted`.
3. **View extracted claims** on a Candidate to see its admitted Claims and
   Protocols, each with its Concepts and a locator deep-link into the video.
4. **Reject** removes a Candidate from the queue; if extraction **failed**,
   **Retry** re-runs it.
5. **Browse the Body of Knowledge** from the top nav — filterable lists of
   **Claims**, **Protocols**, and **Concepts**. Open any of them to follow its
   connections (a Claim's referenced Concepts and supported Protocols; a Protocol's
   justifying Claims; everything that references a Concept), and **edit or delete**
   any Claim or Protocol in place. An edit is marked a *protected version* so a
   later re-extraction won't overwrite your correction (ADR-0005/0010).

The worker and pipeline read the same `.env`; the worker needs the LLM keys
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) to extract and embed. The HTTP API can
also be run outside Docker for development:

```bash
pip install -e ".[web,dev]"
uvicorn health_bok.api:app --reload      # the API
health-bok worker                        # drain the admission queue
```

> **Dependency note:** the Web App pins `next@14.2.x` (latest patched). The
> remaining `npm audit` advisories are Next.js DoS classes fixed only by a major
> upgrade and a transitive PostCSS issue — none apply to a single-user app reached
> only over Tailscale.

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

Adding a Creator also **backfills** its recent back-catalogue as metadata-only
**Candidates** (issue #7): every past upload published within `BACKFILL_CUTOFF_DAYS`
(default ~2 years) is recorded by title, description, publish date, and URL — with
no Transcript fetched and Whisper never called. These Candidates await the owner's
approval into the Body of Knowledge — done in the **Web App** (Part 2), never email;
re-adding a Creator only tops up newly-published ones.

## Schedule the daily job

The job is meant to run unattended each morning (~6am). Two ready-to-edit units
live in [`deploy/`](deploy/) — pick whichever your VPS uses; both just invoke
`health-bok run` on a daily timer and source the deploy's `.env` for secrets.

**cron** — paste [`deploy/crontab.example`](deploy/crontab.example) into `crontab -e`
(adjust the install path and `CRON_TZ`):

```cron
CRON_TZ=UTC
0 6 * * *  cd /opt/health_bok_app && set -a && . ./.env && set +a && .venv/bin/health-bok run >> /var/log/health-bok.log 2>&1
```

**systemd** — install the oneshot service + timer (the timer owns the 06:00
schedule; `Persistent=true` catches a run missed while the VPS was down):

```bash
sudo cp deploy/systemd/health-bok.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now health-bok.timer
```

> Host cron/systemd is the Part-1 standalone path. Under the Part-2 docker-compose
> stack the pipeline instead runs as a **scheduled container** (it runs the job, then
> sleeps a day) alongside the Postgres-backed **worker** (ADR-0009) — no second
> datastore, same Postgres. Use whichever fits your deployment.

## Tests

The primary tests drive the whole system end-to-end with the ports faked and a
**real ephemeral Postgres + pgvector** (spun up via `testcontainers`), asserting on
the persisted records and observable behaviour.

Part 1: `test_daily_job.py` covers detection across Creators, idempotent re-run,
the empty day, per-Creator failure isolation, and the retriable failed send;
`test_transcript_fallback.py` covers the captions-vs-Whisper decision;
`test_backfill.py` covers backfill Candidate population. `test_map_reduce_summarizer.py`
is a pure unit test (no Postgres, no Docker).

Part 2 (issue #13): `test_admission.py` drives Approve → Extract → See Claims —
approval enqueues a job, the worker admits it, and Claims/Protocols/Concepts/edges
land with provenance and locator deep-links (ungroundable assertions dropped,
unstructured "protocols" demoted to Claims, scope qualifiers preserved), plus the
failed-then-retry path. `test_concept_normalization.py` exercises merge-vs-new at
the threshold with a `FakeEmbedder` over real pgvector; `test_edges.py` asserts the
edges integrity trigger rejects dangling endpoints and the unique constraint dedupes
re-asserts; `test_review.py` covers approve/reject queue effects; `test_email_demotion.py`
covers the email off-switch and the Web App deep-link; `test_extractor_parsing.py`
is a pure unit test of the extractor's JSON contract.

Part 2 (issue #14): `test_bok_browse.py` drives Browse & edit the Body of Knowledge —
starting from a genuinely admitted Candidate it asserts the filterable lists, the detail
views resolving connections both ways over `edges` (a Claim's supported Protocols, a
Protocol's justifying Claims, a Concept's referencing entities), an in-place edit that
persists and flags the entity `protected`, the structure CHECK surviving an edit, and a
delete that removes the entity and clears its otherwise-dangling edges.

```bash
source .venv/bin/activate    # after: pip install -e ".[dev]"
pytest                       # needs a running Docker daemon
```
