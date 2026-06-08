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
processing that approval triggers — is Part 2, deferred to later slices.

Everything else — Candidate approval and all of Part 2 (the knowledge graph) — is
deferred to later slices.

## Architecture

A single self-hosted **Postgres** is the only source of truth (ADR-0003). The
raw Transcript is the permanent record; the Summary is disposable and
re-derivable (ADR-0001). No graph/entity extraction runs here.

Four **ports** isolate every external boundary so the job can be tested with
fakes while Postgres stays real:

| Port            | Real adapter (`health_bok/adapters/`) | Service             |
| --------------- | ------------------------------------- | ------------------- |
| `ContentSource` | `youtube.py`                          | YouTube — resolves an @handle/URL to a `channel_id`, lists the back-catalogue for backfill Candidates, discovers new uploads via RSS, fetches captions, downloads audio for the Whisper fallback |
| `Transcriber`   | `whisper.py`                          | OpenAI Whisper — transcribes a caption-less video's audio (daily path only) |
| `Summarizer`    | `claude.py` (single pass), wrapped by `summarizer.py` (map-reduce for long Transcripts) | Claude API          |
| `DigestSender`  | `resend.py`                           | Resend              |

```
health_bok/
├── config.py        env-var configuration (no secrets in code)
├── models.py        domain types (CreatorIdentity, Transcript, Provenance, Summary, Digest)
├── ports.py         the four port protocols
├── db.py            Postgres connection + schema bootstrap
├── schema.sql       creators / candidates / videos / transcripts / summaries / processing_state
├── repository.py    persistence (creators, candidates, archive, summarize, mark processed/sent)
├── creators.py      Creator-management service (add / remove the watch list)
├── backfill.py      backfill Candidate population: list back-catalogue → metadata-only Candidates
├── summarizer.py    MapReduceSummarizer: chunk + reduce long Transcripts
├── job.py           the orchestrator (run_job): RSS detect → spine → one Digest
├── main.py          CLI: `run` (daily job) + `creators add|remove|list`
└── adapters/        youtube · whisper · claude · resend

deploy/              cron + systemd units that run the job daily (~6am)
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
| `RESEND_API_KEY`           | Resend — the DigestSender                          |
| `DIGEST_FROM`              | Verified Resend "from" address                      |
| `DIGEST_RECIPIENT`         | Where the Digest is delivered                       |
| `OPENAI_API_KEY`           | OpenAI Whisper — transcribes caption-less videos    |

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
approval into the Body of Knowledge (a later slice); re-adding a Creator only tops
up newly-published ones.

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

## Tests

The primary tests drive the whole job end-to-end with the three ports faked and a
**real ephemeral Postgres** (spun up via `testcontainers`), asserting on the
persisted records and the captured Digest. `test_daily_job.py` covers detection
across Creators, idempotent re-run, the empty day, per-Creator failure isolation,
and the retriable failed send; `test_transcript_fallback.py` covers the
captions-vs-Whisper decision (captions used when present, Whisper when absent,
and the source recorded either way); `test_backfill.py` covers backfill Candidate
population (metadata-only Candidates, the recency cutoff honored, idempotent
re-runs, and that no transcription happens). They need Docker running.
`test_map_reduce_summarizer.py` is a pure unit test of the chunk/reduce path
(faked Summarizer, no Postgres) and needs no Docker.

```bash
source .venv/bin/activate
pytest
```
