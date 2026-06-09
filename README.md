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
  the personal layer (Goals, Markers, Decisions), queries it all in grounded, cited
  natural language, and is told when new evidence bears on their choices. Tracked in
  slices #13–18 — **all now built**: "Approve → Extract → See Claims" (#13), "Browse &
  edit the Body of Knowledge" (#14), "Creator management & backfill" (#15), "the personal
  layer — Goals, Markers, Decisions" (#16), "grounded natural-language query" (#17), and
  "the Impact engine — bidirectional change detection" (#18) (see below).

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

**Slice 10 — Creator management & backfill in the Web App (issue #15).** The owner now
manages the watch list and pulls in a Creator's back-catalogue entirely from the Web App —
no CLI needed (ADR-0009). Add a Creator by @handle or channel URL (resolved once to a
stable channel_id; an unresolvable one fails loudly inline), see each Creator's resolved
name, **Backfill** its recent back-catalogue, or remove it. The **Backfill** tab is the
back-catalogue review queue of metadata-only Candidates (thumbnail, title, description,
date, link): **bulk-reject** obvious noise, or **Approve** one — approving runs the *same*
pipeline as a daily Candidate, the worker **transcribing-if-needed** (captions, else
Whisper) before extracting and admitting, since a backfill Candidate has no Transcript yet.

**Slice 11 — the personal layer: Goals, Markers, Decisions (issue #16).** The owner-specific
layer of what the owner *wants, measures, and does* (CONTEXT.md "Personal Layer"), recorded
through guided forms and linked to the impersonal evidence layer by **Concept overlap**. The
owner can:

- **Record a Goal** — a stable intention or risk ("lower cardiovascular risk") and the
  Concepts it concerns. A Goal no Decision serves is flagged **unmet**, and a Goal's detail
  shows which Decisions serve it.
- **Record a Marker reading** — value + unit + reference range + date, referencing a Concept.
  Readings are **append-only dated snapshots, never overwritten** (the database rejects any
  mutate), so each Concept's history is a true **time-series**, viewable as a series. **"Out
  of range" is derived** from the stored reference range (one- or two-sided), never a stored
  flag.
- **Record a Decision** — a time-bound adoption carrying its **own actual parameters**
  (the owner's dose/timing), distinct from the Protocol it implements, so **deviation is
  first-class**. **"Adopt"** on a Protocol's detail page pre-fills a Decision and links the
  Protocol it `implements`, inheriting its Concepts.
- **Suggest-then-confirm linking** — the Web App suggests the Protocols, Claims, and Goals
  relevant to a Decision **by Concept overlap** (reusing the Slice-8 Embedder/Concept
  machinery), and the owner **confirms or rejects each one individually**. A Decision also
  links the Goal(s) it serves and the Marker(s) that motivated it, so its **rationale** —
  the supporting evidence and the Goals served — is reviewable in one place.

Concept mentions on a Goal/Marker/Decision are normalized onto the **same** canonical
Concepts the admit pipeline mints, so the personal layer and the Body of Knowledge share one
Concept set and overlap is meaningful. New typed tables `goals`/`markers`/`decisions`; the
`decision → protocol implements`, `decision → goal serves`, `decision → marker motivated_by`,
and `claim → decision supports` relationships live in the one polymorphic `edges` table
(ADR-0008).

**Slice 12 — grounded natural-language query (issue #17).** The primary way the owner now
*explores* the Body of Knowledge (a visual graph is out of v1 scope, ADR-0009/0011). The
owner asks a free-text question in the Web App's **Ask** tab and gets a **synthesized
answer, not a list of hits** — grounded **strictly** in their own library:

- **Strictly grounded & always cited** — the answer draws *only* on the owner's curated
  Body of Knowledge (Claims, Protocols) and personal layer (Goals, Markers, Decisions),
  never the model's general knowledge, and **cites the specific Claims it rests on** — each
  clickable through to its Source and the locator deep-link (`watch?v=ID&t=NNNs`).
- **Abstains honestly** — when nothing in the library covers the question it answers
  "nothing in your library covers this" rather than confabulating. Abstention is
  **structural**: a question that retrieves no Concept within a distance cutoff, or no
  admitted Claim, never reaches the model, and a non-abstaining answer with no resolvable
  citation is itself treated as an abstention (**cite-or-abstain** has no third state).
- **Personal scope included** so answers are actionable — "what are my options for lowering
  apoB given my last reading?" sees the owner's Markers and Decisions.
- **Retrieval reuses existing machinery** — the question is embedded with the *same*
  Embedder the admit pipeline uses, matched to the nearest **Concepts** via pgvector, and
  traversed over `references` edges to the evidence (ADR-0008) — not a new subsystem. The
  synthesis is a new `QueryAnswerer` port (Claude in production; faked in tests).

**Slice 13 — the Impact engine: bidirectional change detection (issue #18).** When
newly-arrived evidence reinforces, contradicts, refines, or opens an opportunity against
the owner's choices — or a new choice meets the existing library — the owner is **told**,
with a reviewable **inbox** and an audit trail (CONTEXT.md "Impact"). Detection fires in
**either direction**:

- **Forward** — a newly-admitted **Claim/Protocol** is checked against the existing
  anchors (Decisions, Goals, Markers). Runs in the worker after admission.
- **Reverse** — a newly-recorded **Decision or Goal** is scanned against the existing Body
  of Knowledge. Runs when the owner records it. An **unmet Goal** is the prime target for
  an `opportunity` Impact.

Either way, candidate pairs are generated by **shared-Concept overlap** (the same
Concept-traversal machinery query is built on, ADR-0008/0011) and a new `StanceJudge`
port — an **LLM pass**, Claude in production — assigns the **Stance**
(`reinforces · contradicts · refines · opportunity`, or `unrelated`, which is discarded).
The judgement, not the overlap, is what raises an Impact, so the inbox surfaces genuine
change rather than everything merely-related.

- **Persisted & deduped** — each Impact is a typed entity (`impacts` table) pointing at a
  polymorphic anchor, unique on `(anchor, source, stance)`, so re-runs and overlapping
  evidence **never nag twice**.
- **Inbox & lifecycle** — the inbox is **filterable by stance and by anchor**; each Impact
  walks `new → reviewed → actioned | dismissed` and, once resolved, **never re-nags** (its
  surviving row also dedups it). A burst (e.g. after a backfill approval) can be
  **bulk-dismissed**.
- **Actioning records the link** — actioning an Impact (typically a `contradicts` or
  `opportunity`) records the **Decision** the owner revised or created in response: change
  detection driving auditable change.
- **Supersede (ADR-0005)** — when a re-extraction can no longer match a superseded Claim,
  an Impact is raised against any **Decision** that Claim supported, so changed evidence
  under a Decision is surfaced, not silently broken.

The `StanceJudge` is faked in tests, so the whole engine is driven over **real**
Concept-overlap candidates from a real Postgres. Detection is **failure-isolated**: a
judge hiccup never undoes a durable admission or fails a Decision/Goal write.

## Architecture

A single self-hosted **Postgres** is the only source of truth (ADR-0003). The
raw Transcript is the permanent record; the Summary is disposable and
re-derivable (ADR-0001). Part 2 layers the knowledge graph onto this same
Postgres (ADR-0008): typed entity tables (`claims`, `protocols`, `concepts`, the
personal `goals`/`markers`/`decisions`, and the change-detection `impacts`), one
polymorphic `edges` table (integrity enforced by trigger, idempotent on a unique
key), an append-only `embeddings` table (pgvector + HNSW), a `jobs` queue, and the
daily-Candidate `admissions` lifecycle. Extraction fills them (ADR-0010).

**Ports** isolate every external boundary so the job and worker can be tested with
fakes while Postgres stays real. Part 1's four, plus Part 2's four:

| Port            | Real adapter (`health_bok/adapters/`) | Service             |
| --------------- | ------------------------------------- | ------------------- |
| `ContentSource` | `youtube.py`                          | YouTube — resolves an @handle/URL to a `channel_id`, lists the back-catalogue for backfill Candidates, discovers new uploads via RSS, fetches captions, downloads audio for the Whisper fallback |
| `Transcriber`   | `whisper.py`                          | OpenAI Whisper — transcribes a caption-less video's audio (daily path only) |
| `Summarizer`    | `claude.py` (single pass), wrapped by `summarizer.py` (map-reduce for long Transcripts) | Claude API          |
| `DigestSender`  | `resend.py`                           | Resend              |
| `Extractor`     | `extractor.py`                        | Claude API — Transcript → Claims + Protocols + Concept mentions (precision-first, ADR-0010) |
| `Embedder`      | `embedder.py`                         | OpenAI `text-embedding-3-small` — 1536-d vectors for Concept normalization (ADR-0008) |
| `QueryAnswerer` | `answerer.py`                         | Claude API — retrieved evidence → a grounded, cited answer or an abstention (ADR-0011) |
| `StanceJudge`   | `stance.py`                           | Claude API — one knowledge↔anchor pair → a Stance for change detection (issue #18) |

```
health_bok/
├── config.py        env-var configuration (no secrets in code)
├── models.py        domain types (Transcript, Provenance, Summary, Digest; Extraction/Claim/Protocol; query + Impact port types)
├── ports.py         the eight port protocols
├── db.py            Postgres connection + schema bootstrap
├── schema.sql       Part 1 (creators/candidates/videos/transcripts/summaries/processing_state)
│                    + Part 2 (concepts/claims/protocols/edges/embeddings/jobs/admissions/goals/markers/decisions/impacts)
├── repository.py    persistence for both parts (one Repository over the one Postgres)
├── creators.py      Creator-management service (add / remove the watch list)
├── backfill.py      backfill Candidate population: list back-catalogue → metadata-only Candidates
├── summarizer.py    MapReduceSummarizer: chunk + reduce long Transcripts
├── job.py           the daily orchestrator (run_job): RSS detect → spine → one Digest
├── review.py        owner-driven Candidate transitions: approve · reject · retry (Part 2)
├── admit.py         extract → ground/structure → normalize Concepts → auto-admit (Part 2)
├── concepts.py      ConceptNormalizer: embed → nearest via pgvector → merge/new (Part 2)
├── curation.py      in-place edit/delete of admitted Claims & Protocols; edit-protection (Part 2)
├── personal.py      record Goals/Markers/Decisions & suggest-then-confirm linking (Part 2)
├── query.py         grounded NL query: retrieve → answer → ground (cite-or-abstain) (Part 2)
├── impacts.py       the Impact engine: forward/reverse/supersede detection + inbox lifecycle (Part 2)
├── worker.py        drains the jobs queue (FOR UPDATE SKIP LOCKED); lifecycle + forward Impact pass (Part 2)
├── api.py           FastAPI HTTP API the Web App calls: review · BoK · personal · query · Impacts (Part 2)
├── main.py          CLI: `run` (daily job) · `worker` (drain queue) · `creators …`
└── adapters/        youtube · whisper · claude · resend · extractor · embedder · answerer · stance

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
| `QUERY_MODEL`              | Claude model for grounded-query synthesis (Part 2; default `claude-sonnet-4-6`) |
| `QUERY_MAX_DISTANCE`       | Cosine-distance abstention cutoff for query retrieval (Part 2; default `0.6`) |
| `STANCE_MODEL`             | Claude model for the Impact StanceJudge (Part 2; default `claude-sonnet-4-6`) |
| `IMPACT_CANDIDATE_LIMIT`   | Per-category cap on candidates a detection pass judges (Part 2; default `25`) |

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
2. **Manage Creators** from the top nav — add by @handle or channel URL (resolved
   once to a stable channel_id; an unresolvable reference fails loudly inline),
   see each Creator's resolved channel name, **Backfill** its back-catalogue, or
   remove it. No CLI needed to feed the pipeline (issue #15).
3. **Backfill** (top nav) is the back-catalogue review queue: metadata-only
   Candidates — thumbnail, title, description, publish date, link — with no preview
   tier. Judge relevance at a glance, **bulk-reject** obvious noise (tick the boxes
   → *Reject selected*), or **Approve** one. Approving runs the *same* pipeline as a
   daily Candidate — the worker **transcribes-if-needed** (captions, else Whisper)
   before extracting and admitting, since a backfill Candidate has no Transcript yet.
4. **Approve** a Candidate — the API enqueues a job and returns immediately; the
   **worker** extracts Claims/Protocols, normalizes Concepts, and auto-admits,
   and you watch the badge walk `approved → processing → admitted`.
5. **View extracted claims** on a Candidate to see its admitted Claims and
   Protocols, each with its Concepts and a locator deep-link into the video.
6. **Reject** removes a Candidate from the queue; if extraction **failed**,
   **Retry** re-runs it.
7. **Browse the Body of Knowledge** from the top nav — filterable lists of
   **Claims**, **Protocols**, and **Concepts**. Open any of them to follow its
   connections (a Claim's referenced Concepts and supported Protocols; a Protocol's
   justifying Claims; everything that references a Concept), and **edit or delete**
   any Claim or Protocol in place. An edit is marked a *protected version* so a
   later re-extraction won't overwrite your correction (ADR-0005/0010).
8. **Record the personal layer** from the top nav (issue #16): **Goals** (with the
   Concepts they concern; unmet ones flagged), **Markers** (append-only dated
   readings per Concept, with out-of-range derived and a per-Concept history series),
   and **Decisions** (with your own actual parameters). On a **Protocol** open
   *Adopt as a Decision* to pre-fill one and link the Protocol it implements; on a
   **Decision** confirm or reject the Protocols, Claims, and Goals it overlaps with
   by Concept, link the Markers that motivated it, and review its whole rationale.
9. **Ask** (top nav, issue #17) is grounded natural-language query — the primary way to
   *explore* the library. Type a question; get a **synthesized, cited answer** drawn
   **only** from your own Claims, Protocols, and personal layer (never general knowledge).
   Each answer **cites the Claims it rests on**, clickable through to the Source and the
   moment in the video; when nothing covers the question it says so rather than guessing.
   The API needs the LLM keys (`OPENAI_API_KEY` to embed the question, `ANTHROPIC_API_KEY`
   to synthesize); `QUERY_MODEL` / `QUERY_MAX_DISTANCE` tune the model and the abstention
   cutoff.
10. **Impacts** (top nav, issue #18) is the change-detection **inbox**. New evidence (a
    just-admitted Claim/Protocol) and new choices (a recorded Decision/Goal) raise
    stance-typed **Impacts** — *reinforces · contradicts · refines · opportunity* — against
    your Decisions, Goals, and Markers. **Filter** by stance and by anchor; **review**,
    **dismiss** (including **bulk-dismiss** a burst), or **action** one — actioning records
    the Decision you revise or create in response. A resolved Impact never re-nags. The API
    needs the LLM keys (`ANTHROPIC_API_KEY` for the StanceJudge, `OPENAI_API_KEY` to embed);
    `STANCE_MODEL` / `IMPACT_CANDIDATE_LIMIT` tune the model and the per-pass candidate cap.

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

Maintain the watch list of Creators the system follows. The **Web App** is the
primary way to do this (the *Creators* tab — add/list/remove and trigger a
backfill; see [Run the Web App](#run-the-web-app-part-2)). The equivalent **CLI**
is an ops/admin convenience and needs only `DATABASE_URL` — not the Digest or
Summarizer secrets.

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
(default ~2 years) is recorded by thumbnail, title, description, publish date, and
URL — with no Transcript fetched and Whisper never called. Re-adding a Creator, or
hitting **Backfill** in the Web App, only tops up newly-published ones.

These Candidates await the owner's approval into the Body of Knowledge — done in
the **Web App** (the *Backfill* tab), never email. **Approving** a backfill
Candidate is where transcribe-if-needed genuinely fires (issue #15): because it
has no archived Transcript, the worker acquires one (free captions, else paid
Whisper) and archives it before extraction → admission — the exact same pipeline a
daily Candidate flows through. Obvious noise can be **bulk-rejected** instead.

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

Part 2 (issue #15): `test_backfill_admission.py` drives Creator management + backfill —
add/list/remove a Creator (resolved channel name shown), an explicit backfill trigger
that surfaces metadata-only Candidates (thumbnail and all), bulk-reject that removes them
and keeps them from resurfacing, and — the heart of the slice — approving a backfill
Candidate with no Transcript so the worker transcribes-if-needed (captions *and* the
caption-less Whisper path) before extracting and admitting, plus the failed-then-retry
path proving the archived Transcript survives and isn't re-transcribed.

Part 2 (issue #16): `test_personal_layer.py` drives the personal layer — starting from a
genuinely admitted Candidate it asserts Goal/Marker/Decision CRUD; a Marker being
append-only (the immutability trigger rejects an UPDATE *and* a DELETE) with out-of-range
derived from one- and two-sided reference ranges and a viewable history series; a Decision
carrying its own actual parameters distinct from the Protocol it adopts; and
suggest-then-confirm links generated by Concept overlap (a relevant Protocol, Claim, and
Goal), individually confirmable so a confirmed one drops from the next round, with the
Decision's rationale (supporting Claims + served Goals + motivating Markers) reviewable.

Part 2 (issue #17): `test_query.py` drives grounded natural-language query over a real
Postgres with only the `QueryAnswerer` faked — a question with coverage gets a synthesized
answer citing only admitted Claims (clickable to Source + locator); one without coverage
abstains with the canonical message and never calls the answerer; retrieval surfaces
personal-layer context; a hallucinated citation is dropped; and the answerer's own
abstention is honored. `test_extractor_parsing.py`'s sibling, the answerer's JSON contract,
is exercised through the same fake.

Part 2 (issue #18): `test_impacts.py` drives the Impact engine over a real Postgres with
only the `StanceJudge` faked, asserting bidirectional triggering (a new Claim/Protocol vs
existing anchors; a new Decision/Goal vs the Body of Knowledge), the `unrelated` discard
(overlap alone never raises), Markers as anchors, dedup across re-runs, the inbox filters,
the `new → reviewed → actioned | dismissed` lifecycle with no re-nag and bulk-dismiss,
actioning recording the resulting Decision link, and the supersede case (ADR-0005).
`test_stance_parsing.py` is a pure unit test of the StanceJudge adapter's JSON contract —
every valid Stance round-trips and anything unrecognized collapses to `unrelated`.

```bash
source .venv/bin/activate    # after: pip install -e ".[dev]"
pytest                       # needs a running Docker daemon
```
