# Health & Longevity Knowledge System

A personal system that monitors health & longevity content creators, archives and
summarizes their material, and links it into a personalized knowledge graph connecting
evidence to your own health decisions, markers, and goals. You work through a self-hosted
**Web App**; the daily email **Digest** is only a notification that links back into it. See
[`CONTEXT.md`](CONTEXT.md) for the domain language and [`docs/adr/`](docs/adr/) for the
architectural decisions.

## What it does

The system has two parts: a **daily pipeline** that brings new content in, and a **Web App**
where you turn that content into a curated, personal knowledge graph.

### The daily pipeline

Watches your list of **Creators**, detects their new uploads, and runs each one through a
spine:

1. fetches the video's **Transcript** — free YouTube captions when present, falling back to
   the OpenAI **Whisper** API (audio download → transcription) when a video has none;
2. archives that Transcript **immutably** in Postgres with full provenance;
3. generates a prose **Summary** via the Claude API, map-reducing transcripts longer than a
   configurable threshold (chunk → summarize → reduce) so a multi-hour podcast never breaks
   the run; and
4. bundles the run's new Summaries into **one Digest** email — sent only when there is new
   content.

The run is **idempotent**: a video is marked processed only after its Transcript *and*
Summary are persisted, so re-running reprocesses nothing and sends no second email. A failed
Digest send retries on the next run without re-summarizing. One Creator's error (an
unreachable feed, a missing transcript) is isolated and never aborts the rest of the run.

### The Web App & knowledge graph

The primary interface. It loads over Tailscale with **no login** — the tailnet is the auth
boundary. In it you:

- **Review and approve Candidates.** Each new upload — and each backfilled back-catalogue
  item — appears as a Candidate with its Summary. Approving it enqueues a background job and
  returns immediately; the worker walks the Candidate `approved → processing → admitted`.
- **Build the Body of Knowledge through extraction.** The worker extracts **Claims** and
  **Protocols** precision-first (scope qualifiers preserved; grounded-or-dropped with
  locator deep-links back to the moment asserted; Protocols only when structured —
  action + dose/timing/frequency/duration, else the assertion stays a Claim), normalizes
  **Concepts** via embeddings (nearest-match merge, else mint a new Concept), and
  auto-admits.
- **Browse & edit** filterable lists of Claims, Protocols, and Concepts. Follow connections
  by navigation — a Claim's referenced Concepts and supported Protocols, a Protocol's
  justifying Claims, everything that references a Concept — each keeping its locator
  deep-link back into the source. Every admitted Claim and Protocol can be edited or deleted
  in place; an edit is marked a **protected version** so a later re-extraction won't clobber
  your correction.
- **Manage Creators & backfill.** Add a Creator by @handle or channel URL (resolved once to
  a stable channel_id), see its resolved name, pull in its recent back-catalogue as
  metadata-only Candidates, or remove it. Bulk-reject obvious noise; approving a backfill
  Candidate runs the *same* pipeline as a daily one (transcribing-if-needed first).
- **Record the personal layer.** **Goals** (intentions or risks and the Concepts they
  concern — editable on the Goal's page after creation, by picking from the catalogue or
  typing a new term normalized onto one canonical Concept set; the page also suggests
  Concepts the Goal likely concerns, inferred from its title + detail — *existing* ones
  matched over pgvector, and *new* ones an LLM proposes that resolve to nothing in the
  catalogue, the two set apart in the UI and each confirmable in one click (confirming a
  new one mints the Concept; nothing is minted without your confirmation); unmet Goals
  flagged),
  **Markers** (append-only dated readings per Concept, with
  out-of-range *derived* from the stored reference range and a viewable history series), and
  **Decisions** (time-bound adoptions carrying your *own* actual parameters, so deviation
  from a Protocol is first-class). The Web App suggests the Protocols, Claims, and Goals
  relevant to a Decision by Concept overlap, for you to confirm or reject individually.
- **Ask** free-text questions and get a **synthesized, cited answer** — grounded strictly in
  your own Claims, Protocols, and personal layer, never the model's general knowledge. Each
  answer cites the Claims it rests on (clickable through to the Source and the moment in the
  video) and **abstains honestly** when nothing in your library covers the question.
- **Triage the Impacts inbox.** When new evidence bears on your choices — or a new choice
  meets the existing library — a stance-typed **Impact** (*reinforces · contradicts ·
  refines · opportunity*) is raised against the relevant Decision, Goal, or Marker. Filter
  by stance and anchor; review, dismiss (or bulk-dismiss a burst), or **action** one —
  actioning records the Decision you revise or create in response. A resolved Impact never
  re-nags.
- **See the Logs.** A read-only record of every video Source that reached a terminal
  admission, newest-first — each with its title, its Creator, the date it was added, a snippet
  of its latest Summary, and a **BoK-state** badge (*admitted · failed*) distinguishing what
  reached the Body of Knowledge from what failed extraction. Videos still in flight or never
  approved are not listed. It makes the pipeline's dedup guard visible — a video is never
  reprocessed twice — and links each row through to that video's Claims. It has no actions.

The Digest is just a notification that deep-links into the Web App's review queue; set
`DIGEST_ENABLED=false` and the system stays fully usable with email off.

## Architecture

A single self-hosted **Postgres** is the only source of truth. The raw Transcript is the
permanent record; the Summary is disposable and re-derivable. The knowledge graph layers
onto the same Postgres: typed entity tables (`claims`, `protocols`, `concepts`, the personal
`goals`/`markers`/`decisions`, and the change-detection `impacts`), one polymorphic `edges`
table (integrity enforced by trigger, idempotent on a unique key), an append-only
`embeddings` table (pgvector + HNSW), a `jobs` queue, and the Candidate `admissions`
lifecycle.

**Ports** isolate every external boundary, so the job and worker can be tested with fakes
while Postgres stays real:

| Port            | Real adapter (`health_bok/adapters/`) | Service             |
| --------------- | ------------------------------------- | ------------------- |
| `ContentSource` | `youtube.py`                          | YouTube — resolves an @handle/URL to a `channel_id`, lists the back-catalogue for backfill Candidates, lazily fetches one Candidate's full description + accurate publish date on demand, discovers new uploads via RSS, fetches captions, downloads audio for the Whisper fallback |
| `Transcriber`   | `whisper.py`                          | OpenAI Whisper — transcribes a caption-less video's audio (daily path only) |
| `Summarizer`    | `claude.py` (single pass), wrapped by `summarizer.py` (map-reduce for long Transcripts) | Claude API          |
| `DigestSender`  | `resend.py`                           | Resend              |
| `Extractor`     | `extractor.py`                        | Claude API — Transcript → Claims + Protocols + Concept mentions (precision-first) |
| `Embedder`      | `embedder.py`                         | OpenAI `text-embedding-3-small` — 1536-d vectors for Concept normalization |
| `QueryAnswerer` | `answerer.py`                         | Claude API — retrieved evidence → a grounded, cited answer or an abstention |
| `StanceJudge`   | `stance.py`                           | Claude API — one knowledge↔anchor pair → a Stance for change detection |
| `ConceptProposer` | `concept_proposer.py`               | Claude API — a Goal's title + detail → candidate new-Concept terms (owner-confirmed before minting) |

```
health_bok/
├── config.py        env-var configuration (no secrets in code)
├── models.py        domain types (Transcript, Provenance, Summary, Digest; Extraction/Claim/Protocol; query + Impact port types)
├── ports.py         the eight port protocols
├── db.py            Postgres connection + schema bootstrap
├── schema.sql       creators/candidates/videos/transcripts/summaries/processing_state
│                    + concepts/claims/protocols/edges/embeddings/jobs/admissions/goals/markers/decisions/impacts
├── repository.py    persistence over the one Postgres
├── creators.py      Creator-management service (add / remove the watch list)
├── backfill.py      backfill Candidate population: list back-catalogue → metadata-only Candidates
├── summarizer.py    MapReduceSummarizer: chunk + reduce long Transcripts
├── job.py           the daily orchestrator (run_job): RSS detect → spine → one Digest
├── review.py        owner-driven Candidate transitions: approve · reject · retry
├── admit.py         extract → ground/structure → normalize Concepts → auto-admit
├── concepts.py      ConceptNormalizer: embed → nearest via pgvector → merge/new
├── curation.py      in-place edit/delete of admitted Claims & Protocols; edit-protection
├── personal.py      record Goals/Markers/Decisions & suggest-then-confirm linking
├── query.py         grounded NL query: retrieve → answer → ground (cite-or-abstain)
├── impacts.py       the Impact engine: forward/reverse/supersede detection + inbox lifecycle
├── worker.py        drains the jobs queue (FOR UPDATE SKIP LOCKED); lifecycle + forward Impact pass
├── api.py           FastAPI HTTP API the Web App calls: review · BoK · personal · query · Impacts
├── main.py          CLI: `run` (daily job) · `worker` (drain queue) · `creators …`
└── adapters/        youtube · whisper · claude · resend · extractor · embedder · answerer · stance

web/                 the Next.js Web App — review queue, BoK browser, personal layer, Ask, Impacts, Logs
Dockerfile           Python image: API · worker · scheduled pipeline (one image, three commands)
docker-compose.yml   db (pgvector) · api · worker · web · pipeline
deploy/              cron + systemd units (the host-scheduling alternative to the scheduled container)
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
| `EXTRACTION_MODEL`         | Claude model for extraction (default `claude-sonnet-4-6`) |
| `EMBEDDING_MODEL`          | Embedding model for Concept normalization (default `text-embedding-3-small`) |
| `CONCEPT_MERGE_DISTANCE`   | Cosine-distance merge threshold for Concepts (default `0.15`) |
| `WEBAPP_BASE_URL`          | Web App base URL for Digest deep-links (optional)  |
| `QUERY_MODEL`              | Claude model for grounded-query synthesis (default `claude-sonnet-4-6`) |
| `QUERY_MAX_DISTANCE`       | Cosine-distance abstention cutoff for query retrieval (default `0.6`) |
| `STANCE_MODEL`             | Claude model for the Impact StanceJudge (default `claude-sonnet-4-6`) |
| `IMPACT_CANDIDATE_LIMIT`   | Per-category cap on candidates a detection pass judges (default `25`) |
| `CONCEPT_PROPOSAL_MODEL`   | Claude model for proposing new Concepts for a Goal (default `claude-sonnet-4-6`) |

## Run the pipeline

```bash
source .venv/bin/activate
health-bok            # or: health-bok run, or: python -m health_bok
```

This applies the schema if needed, then runs the daily job across **every** watched Creator
(see [Manage Creators](#manage-creators) to populate the list): fetch each Creator's RSS
feed, find new uploads by diffing against the already-processed set, run the new ones through
the spine (Transcript → archive → Summary), and email one Digest when there is new content.

## Run the Web App

The whole stack runs under docker-compose: Postgres (pgvector), the HTTP API, the Next.js
Web App, the worker, and the daily pipeline as a scheduled container.

```bash
cp .env.example .env        # fill in the secrets (or set DIGEST_ENABLED=false)
docker compose up --build   # db + api + web + worker + pipeline
```

Then open the Web App at **http://localhost:3000**. In production, bind the published ports
to your **Tailscale** address — the tailnet is the auth boundary, so there is no login
screen. From the top nav you can work the review queue, manage Creators and backfill, browse
and edit the Body of Knowledge, record the personal layer, **Ask** grounded questions, triage
the **Impacts** inbox, and review the **Logs** of every processed video (see
[What it does](#what-it-does)).

The worker and pipeline read the same `.env`; the worker needs the LLM keys
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) to extract and embed. The HTTP API can also be run
outside Docker for development:

```bash
pip install -e ".[web,dev]"
uvicorn health_bok.api:app --reload      # the API
health-bok worker                        # drain the admission queue
```

> **Dependency note:** the Web App pins `next@14.2.x` (latest patched). The remaining
> `npm audit` advisories are Next.js DoS classes fixed only by a major upgrade and a
> transitive PostCSS issue — none apply to a single-user app reached only over Tailscale.

## Logging & observability

The Python services use the standard library `logging` module — no log files, no external
aggregator (this is a single-user app). Each module owns a named logger under the
`health_bok.*` namespace (`health_bok.worker`, `health_bok.review`, `health_bok.admit`,
`health_bok.impacts`, …), so you can tell at a glance which stage emitted a line. The level
is **INFO**, configured once in the CLI entrypoint (`health_bok/main.py`), and every line is
written to **stdout/stderr** in the format `LEVEL name message`.

Because logs go to the standard streams, under docker-compose they are captured per service.
Follow a service's logs with:

```bash
docker compose logs -f worker     # the admission queue: drains, failures, dropped claims
docker compose logs -f api        # the HTTP API serving the Web App
docker compose logs -f pipeline   # the daily run: discovery, summaries, the Digest
```

The lines that matter operationally:

- **`health_bok.review`** — `approved <video>; admission job enqueued`. Approving a Candidate
  only *enqueues* a job and returns; the slow work happens elsewhere (next bullet).
- **`health_bok.worker`** — `worker started …`, `worker drained N job(s)`, and on failure
  `admission failed for <video>: <error>`. This is the process that actually turns an
  approved Candidate into Claims/Protocols. **If the worker isn't running, approvals queue
  up but the Body of Knowledge never changes.**
- **`health_bok.admit`** — admission outcomes and any `dropping ungroundable claim/protocol`.
- **`health_bok.impacts`** — `impact detection … raised N impact(s)`.

Failures are also **persisted**, not just logged: a failed admission records the error on the
`jobs` row (`last_error`) and drives the Candidate to a `failed` state with the same message
(`admissions.error`), both surfaced in the Web App's review queue. So when a Candidate doesn't
admit, there are two places to look — the Web App's failed state, and `docker compose logs
worker` for the full traceback. A `failed` Candidate is retryable from the Web App.

> Every long-running service restarts on failure (`restart: unless-stopped`), and schema
> bootstrap takes a Postgres advisory lock so simultaneous boots can't collide. Still, if
> approvals never take effect, the first thing to check is that the `worker` is actually up:
> `docker compose ps` (a missing or `Exited` `worker` means nothing is draining the queue).

## Manage Creators

Maintain the watch list of Creators the system follows. The **Web App** is the primary way
to do this (the *Creators* tab — add/list/remove and trigger a backfill). The equivalent
**CLI** is an ops/admin convenience and needs only `DATABASE_URL`:

```bash
health-bok creators add @hubermanlab                          # by @handle
health-bok creators add https://www.youtube.com/@PeterAttiaMD # or by channel URL
health-bok creators list                                      # channel_id <TAB> name
health-bok creators remove UC2D2CMWXMOVWx7giW1n3LIg           # by channel_id (from list)
```

On `add`, the @handle/URL is resolved to its YouTube `channel_id` **once** and stored with
the Creator's name; the daily job thereafter keys off that stable `channel_id` and never
re-resolves. Adding the same Creator again — even via a different handle or URL — refreshes
the name but creates no duplicate. Removal is by `channel_id`, so it stays reliable even if a
channel later changes its handle.

Adding a Creator also **backfills** its recent back-catalogue as metadata-only **Candidates**:
every past upload published within `BACKFILL_CUTOFF_DAYS` (default ~2 years) is recorded by
thumbnail, title, description, publish date, and URL — no Transcript fetched, Whisper never
called. Re-adding a Creator, or hitting **Backfill** in the Web App, only tops up
newly-published ones. These Candidates await approval in the Web App's *Backfill* tab; on
approval the worker acquires a Transcript (free captions, else Whisper) before extraction.

The back-catalogue is listed in one cheap pass, which carries no per-video description and
only a best-effort publish date. Each Candidate therefore has a **Fetch details** action: it
runs a single per-video lookup that loads the real description and the accurate publish date,
stores both, and shows them in place — the expensive per-video call happens only when you ask.
The *Backfill* tab can be **sorted by publish date** (newest- or oldest-first), and the
ordering sharpens for any Candidate whose details you've fetched.

## Schedule the daily job

The job is meant to run unattended each morning (~6am). Under the docker-compose stack the
pipeline already runs as a **scheduled container** (it runs the job, then sleeps a day)
alongside the Postgres-backed worker. For a standalone (non-Docker) deployment, two
ready-to-edit units live in [`deploy/`](deploy/) — pick whichever your VPS uses; both invoke
`health-bok run` on a daily timer and source the deploy's `.env`.

**cron** — paste [`deploy/crontab.example`](deploy/crontab.example) into `crontab -e`
(adjust the install path and `CRON_TZ`):

```cron
CRON_TZ=UTC
0 6 * * *  cd /opt/health_bok_app && set -a && . ./.env && set +a && .venv/bin/health-bok run >> /var/log/health-bok.log 2>&1
```

**systemd** — install the oneshot service + timer (`Persistent=true` catches a run missed
while the VPS was down):

```bash
sudo cp deploy/systemd/health-bok.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now health-bok.timer
```

## Tests

The tests drive the whole system end-to-end with the ports faked and a **real ephemeral
Postgres + pgvector** (spun up via `testcontainers`), asserting on the persisted records and
observable behaviour. A handful of pure unit tests (map-reduce chunking, and the
extractor/answerer/stance JSON contracts) need neither Postgres nor Docker.

```bash
source .venv/bin/activate    # after: pip install -e ".[dev]"
pytest                       # needs a running Docker daemon
```
