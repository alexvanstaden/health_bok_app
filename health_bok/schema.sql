-- Slice-1 schema: the single self-hosted Postgres source of truth (ADR-0003).
-- Holds Creators, archived Transcripts + provenance, Summaries, and per-video
-- processing state. No structured/graph extraction is built here (ADR-0001);
-- the schema only leaves clean room to batch-extract over Transcripts later.
--
-- Idempotent: safe to run on every startup.

CREATE TABLE IF NOT EXISTS creators (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id  TEXT        NOT NULL UNIQUE,
    name        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The watch-list flag (issue #69): a *subscribed* Creator is one the owner added
-- and the daily job polls; an *unsubscribed* one exists only to attribute a one-off
-- "Process me" playlist video (its Claims still count toward Strength at the default
-- trust-tier), and is never polled or backfilled. Defaults TRUE so every Creator that
-- predates this column stays on the watch list; `list_creators()` and `/api/creators`
-- read only subscribed Creators. Idempotent add for a database created before #69.
ALTER TABLE creators ADD COLUMN IF NOT EXISTS subscribed BOOLEAN NOT NULL DEFAULT TRUE;

-- A backfill Candidate: a back-catalogue video known only by metadata, awaiting
-- the owner's approval into the Body of Knowledge (CONTEXT.md, ADR-0004). Seeded
-- when a Creator is added (issue #7). Deliberately holds no Transcript or Summary
-- and has no FK to `videos` — backfill never transcribes (user story 29); raw
-- content is acquired only if and when the Candidate is approved (a later slice).
CREATE TABLE IF NOT EXISTS candidates (
    id           BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id     TEXT        NOT NULL UNIQUE,            -- YouTube's own id
    creator_id   BIGINT      NOT NULL REFERENCES creators (id),
    url          TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    description  TEXT        NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,                   -- publish date
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A video Source and its full provenance (PRD #1, user story 13).
CREATE TABLE IF NOT EXISTS videos (
    video_id          TEXT        PRIMARY KEY,            -- YouTube's own id
    creator_id        BIGINT      NOT NULL REFERENCES creators (id),
    url               TEXT        NOT NULL,
    title             TEXT        NOT NULL,
    published_at      TIMESTAMPTZ NOT NULL,               -- publish date
    retrieved_at      TIMESTAMPTZ NOT NULL,               -- retrieved date
    transcript_source TEXT        NOT NULL,               -- 'captions' | 'whisper'
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The immutable raw content — the permanent source of truth (ADR-0001).
-- Segments keep their timestamps (JSONB) for later deep-links (user story 12).
-- One Transcript per video; immutability is enforced by trigger below.
CREATE TABLE IF NOT EXISTS transcripts (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id   TEXT        NOT NULL UNIQUE REFERENCES videos (video_id),
    segments   JSONB       NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A disposable, re-derivable prose write-up (ADR-0001). Append-only: a video may
-- be re-summarized later against a better model, so it is not unique on video_id.
CREATE TABLE IF NOT EXISTS summaries (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id   TEXT        NOT NULL REFERENCES videos (video_id),
    body       TEXT        NOT NULL,
    model      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-video processing lifecycle. "Processed" (transcript + summary persisted)
-- is tracked separately from "digest sent" so a failed email can be retried
-- without re-summarizing (PRD #1, user stories 22, 24).
CREATE TABLE IF NOT EXISTS processing_state (
    video_id              TEXT        PRIMARY KEY REFERENCES videos (video_id),
    transcript_archived_at TIMESTAMPTZ,
    summarized_at         TIMESTAMPTZ,
    digest_sent_at        TIMESTAMPTZ
);

-- Enforce Transcript immutability at the database (ADR-0001): once archived, a
-- Transcript row may not be updated or deleted.
CREATE OR REPLACE FUNCTION transcripts_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'transcripts are immutable (ADR-0001): % blocked', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS transcripts_no_mutate ON transcripts;
CREATE TRIGGER transcripts_no_mutate
    BEFORE UPDATE OR DELETE ON transcripts
    FOR EACH ROW EXECUTE FUNCTION transcripts_immutable();


-- ===========================================================================
-- Part 2: the Web App & Knowledge Graph (issue #13, PRD #12).
--
-- The first vertical — "Approve → Extract → See Claims" — needs the physical
-- knowledge-graph schema (ADR-0008), a Postgres-backed job queue drained by the
-- worker (ADR-0009), and the daily-Candidate admission lifecycle (ADR-0004,
-- ADR-0007, ADR-0010). All added here, idempotently, to the one source-of-truth
-- Postgres (ADR-0003); later slices fill out the personal layer and Impacts.
-- ===========================================================================

-- pgvector backs Concept-normalization and (later) semantic search over the
-- extracted layer (ADR-0008). Idempotent; the container image ships the
-- extension. Claims/Concepts/Protocols are embedded — never raw Transcripts.
CREATE EXTENSION IF NOT EXISTS vector;

-- A normalized, deduplicated hub node (CONTEXT.md "Concept"): a supplement, body
-- system, mechanism, condition, or intervention that Claims and Protocols
-- reference. Unlike Claims, Concepts MAY be merged/normalized — that is their
-- purpose; normalization keys off the Concept's embedding (ADR-0008).
CREATE TABLE IF NOT EXISTS concepts (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT        NOT NULL,
    kind       TEXT,                                       -- supplement | mechanism | …
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A Claim: a single falsifiable assertion drawn from a Source, the atomic unit
-- of the Body of Knowledge (CONTEXT.md, ADR-0002). Provenance is a real FK to
-- the video Source; the locator (seconds into the video) deep-links back to the
-- exact moment (`watch?v=ID&t=NNNs`, ADR-0010). Sub-kinds are a `type` attribute,
-- not separate tables (ADR-0002).
CREATE TABLE IF NOT EXISTS claims (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id        TEXT        NOT NULL REFERENCES videos (video_id),
    text            TEXT        NOT NULL,
    type            TEXT        NOT NULL DEFAULT 'finding'
                                CHECK (type IN ('mechanism', 'principle', 'finding')),
    locator_seconds INTEGER     NOT NULL CHECK (locator_seconds >= 0),
    -- An owner edit in the Web App makes a Claim a *protected version*, not raw
    -- extractor output: a later re-extraction supersede pass (ADR-0005) must read
    -- this flag and not silently clobber it (ADR-0010). Auto-admitted Claims are
    -- unprotected; the in-place edit sets it (issue #14).
    protected       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claims_by_video ON claims (video_id);
ALTER TABLE claims ADD COLUMN IF NOT EXISTS protected BOOLEAN NOT NULL DEFAULT FALSE;

-- A Protocol: a parameterized recommendation with structure — action plus at
-- least one of dose/timing/frequency/duration (CONTEXT.md, ADR-0010). Vague
-- advice never reaches this table: the admit step demotes an unstructured
-- "protocol" to a Claim. Like a Claim it is attributed to a Source and carries a
-- locator. The structure CHECK enforces the contract at the database.
CREATE TABLE IF NOT EXISTS protocols (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id        TEXT        NOT NULL REFERENCES videos (video_id),
    action          TEXT        NOT NULL,
    dose            TEXT,
    timing          TEXT,
    frequency       TEXT,
    duration        TEXT,
    locator_seconds INTEGER     NOT NULL CHECK (locator_seconds >= 0),
    -- Like a Claim, an owner-edited Protocol is a protected version (ADR-0010);
    -- re-extraction (ADR-0005) must not overwrite it (issue #14).
    protected       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (dose IS NOT NULL OR timing IS NOT NULL
           OR frequency IS NOT NULL OR duration IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS protocols_by_video ON protocols (video_id);
ALTER TABLE protocols ADD COLUMN IF NOT EXISTS protected BOOLEAN NOT NULL DEFAULT FALSE;

-- The one polymorphic edges table: every genuinely graph-shaped relationship —
-- the many:many, computed, or traversable ones — lives here (ADR-0008).
-- 1:many ownership (a Claim's Source) stays an FK column above, never an edge.
-- The unique constraint makes edge-writes idempotent so re-extraction (ADR-0005)
-- re-asserts without dup-checking; `kind` is CHECK-constrained, not an ENUM.
CREATE TABLE IF NOT EXISTS edges (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    src_type   TEXT        NOT NULL,
    src_id     BIGINT      NOT NULL,
    dst_type   TEXT        NOT NULL,
    dst_id     BIGINT      NOT NULL,
    kind       TEXT        NOT NULL
                           CHECK (kind IN ('references', 'supports', 'implements',
                                           'serves', 'motivated_by')),
    props      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src_type, src_id, dst_type, dst_id, kind)
);
CREATE INDEX IF NOT EXISTS edges_by_src ON edges (src_type, src_id);
CREATE INDEX IF NOT EXISTS edges_by_dst ON edges (dst_type, dst_id);

-- Polymorphic src/dst can't be real FKs, so referential integrity is enforced by
-- a fail-loud trigger (ADR-0008) — the same pattern transcript immutability uses.
-- Each endpoint type maps to its node table; an endpoint pointing at a row that
-- does not exist is rejected, so no dangling edges accumulate. Node types are
-- extended here as the personal layer (decisions/goals/markers) lands.
CREATE OR REPLACE FUNCTION edges_endpoint_exists(node_type TEXT, node_id BIGINT)
RETURNS BOOLEAN AS $$
DECLARE
    found BOOLEAN;
    tbl   TEXT;
BEGIN
    tbl := CASE node_type
        WHEN 'claim'    THEN 'claims'
        WHEN 'protocol' THEN 'protocols'
        WHEN 'concept'  THEN 'concepts'
        WHEN 'goal'     THEN 'goals'
        WHEN 'marker'   THEN 'markers'
        WHEN 'decision' THEN 'decisions'
        ELSE NULL
    END;
    IF tbl IS NULL THEN
        RAISE EXCEPTION 'edges: unknown node type %', node_type;
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I WHERE id = $1)', tbl)
        INTO found USING node_id;
    RETURN found;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION edges_integrity()
RETURNS TRIGGER AS $$
BEGIN
    IF NOT edges_endpoint_exists(NEW.src_type, NEW.src_id) THEN
        RAISE EXCEPTION 'edges: dangling src % %', NEW.src_type, NEW.src_id;
    END IF;
    IF NOT edges_endpoint_exists(NEW.dst_type, NEW.dst_id) THEN
        RAISE EXCEPTION 'edges: dangling dst % %', NEW.dst_type, NEW.dst_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS edges_no_dangling ON edges;
CREATE TRIGGER edges_no_dangling
    BEFORE INSERT OR UPDATE ON edges
    FOR EACH ROW EXECUTE FUNCTION edges_integrity();

-- One append-only, model-stamped embeddings table over the extracted layer
-- (ADR-0008): re-embedding against a better model is clean, in the spirit of the
-- immutable Transcript (ADR-0001). HNSW + cosine for nearest-Concept lookup.
CREATE TABLE IF NOT EXISTS embeddings (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_type TEXT        NOT NULL,                       -- 'concept' | 'claim' | 'protocol'
    owner_id   BIGINT      NOT NULL,
    embedding  VECTOR(1536) NOT NULL,
    model      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS embeddings_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS embeddings_by_owner ON embeddings (owner_type, owner_id);

-- The Postgres-backed job queue drained by the worker (ADR-0009): approval
-- enqueues long work (extract → normalize → admit) and returns immediately, so a
-- request never blocks. No Redis/Celery — "single Postgres" (ADR-0003) stays
-- literally true. Drained with SELECT … FOR UPDATE SKIP LOCKED.
-- `video_id` is YouTube's stable external id, deliberately *not* an FK to
-- `videos`: a backfill Candidate (issue #15) is approved — enqueuing a job — while
-- it is still metadata-only, before its Source is archived. The worker acquires
-- the Transcript (transcribe-if-needed) when it drains the job, creating the
-- `videos` row then. An FK here would forbid that ordering.
CREATE TABLE IF NOT EXISTS jobs (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       TEXT        NOT NULL CHECK (kind IN ('admit')),
    video_id   TEXT        NOT NULL,
    state      TEXT        NOT NULL DEFAULT 'queued'
                           CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts   INTEGER     NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_queued ON jobs (id) WHERE state = 'queued';
-- Drop the FK on a database first created before issue #15 (idempotent).
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_video_id_fkey;

-- The daily-Candidate admission lifecycle (CONTEXT.md "Candidate"; ADR-0004,
-- ADR-0007, ADR-0010). A daily Candidate is a video already processed (transcript
-- + summary) but not yet admitted to the Body of Knowledge. The owner's video-
-- grain approval is the only human gate; extraction then auto-admits (ADR-0010):
--
--   candidate → approved → processing → admitted
--                    └────────────────→ failed     (extraction error; retryable)
--   candidate → rejected                            (owner declines)
--
-- The absence of a row means a plain, un-acted-on `candidate`; a row records
-- where the owner's decision and the worker have taken it since. The lifecycle is
-- shared by daily and backfill Candidates (issue #15): keyed by the external
-- video_id, *not* an FK to `videos`, so a backfill Candidate can be approved or
-- rejected while still metadata-only — its `videos` row is created later, when the
-- worker acquires the Transcript on approval.
CREATE TABLE IF NOT EXISTS admissions (
    video_id   TEXT        PRIMARY KEY,
    state      TEXT        NOT NULL
                           CHECK (state IN ('approved', 'processing',
                                            'admitted', 'failed', 'rejected')),
    error      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Drop the FK on a database first created before issue #15 (idempotent).
ALTER TABLE admissions DROP CONSTRAINT IF EXISTS admissions_video_id_fkey;


-- ===========================================================================
-- Slice 11: the personal layer (issue #16, PRD #12).
--
-- The owner-specific layer of what the owner wants, measures, and does
-- (CONTEXT.md "Personal Layer"): Goals, Markers, Decisions. Each is a typed
-- entity table (ADR-0008); the Concepts each one concerns/references are recorded
-- as `references` edges (a Marker's single Concept is 1:many *ownership*, so it is
-- an FK column, not an edge — ADR-0008), and a Decision's structural links to the
-- Protocol it implements, the Goal(s) it serves, and the Marker(s) that motivated
-- it all live in the one polymorphic `edges` table. No new edge `kind`s are needed
-- — `references`/`supports`/`implements`/`serves`/`motivated_by` were reserved when
-- `edges` was first created; the integrity trigger above is extended to know the
-- three new node types.
-- ===========================================================================

-- A Goal: a stable personal intention or risk the owner wants to address
-- (CONTEXT.md "Goal") — "improve sleep", "lower cardiovascular risk". The
-- Concepts it concerns are `goal -> concept references` edges, so a Goal and a
-- Decision can be found relevant to each other by shared Concept. An *unmet* Goal
-- — one no Decision `serves` — is visible by the absence of an inbound `serves`
-- edge, not a stored flag.
CREATE TABLE IF NOT EXISTS goals (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title      TEXT        NOT NULL,
    detail     TEXT,                                       -- optional elaboration
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A Marker reading: an objective, quantitative, dated snapshot the owner records,
-- referencing exactly one Concept (apoB, hsCRP, fasting glucose…) (CONTEXT.md
-- "Marker"). Strictly a time-series — every reading is a new row, never an
-- overwrite (the immutability trigger below enforces it), so a Concept's whole
-- history is the rows sharing its `concept_id` ordered by `measured_at`. The
-- reference range is stored (either or both bounds may be absent for a one-sided
-- range like "< 1.0"); "out of range" is *derived* from it on read, never a stored
-- flag. The Concept is 1:many ownership, so it is an FK column, not an edge
-- (ADR-0008). `value` is NUMERIC so series math and range comparison stay exact.
CREATE TABLE IF NOT EXISTS markers (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_id      BIGINT      NOT NULL REFERENCES concepts (id),
    value           NUMERIC     NOT NULL,
    unit            TEXT        NOT NULL,
    reference_low   NUMERIC,                               -- lower bound, if any
    reference_high  NUMERIC,                               -- upper bound, if any
    measured_at     TIMESTAMPTZ NOT NULL,                  -- the reading's date
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS markers_series ON markers (concept_id, measured_at);

-- A Marker reading is an immutable dated snapshot (CONTEXT.md "Marker"): once
-- recorded it is never overwritten or removed, so the time-series stays a faithful
-- audit of what was measured when. Enforced at the database with the same fail-loud
-- pattern transcript immutability (ADR-0001) uses; a correction is a *new* reading.
--
-- The one permitted UPDATE is a pure *hub re-point*: a Concept merge (ADR-0014,
-- issue #86) folds one hub onto another and a reading's `concept_id` must follow so
-- nothing is lost. That leaves the measurement snapshot itself — value, unit,
-- reference range, date — untouched, so it is a graph normalization, not a mutation
-- of what was measured. Any UPDATE that also touches the snapshot is still blocked.
CREATE OR REPLACE FUNCTION markers_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.id = OLD.id
       AND NEW.concept_id IS DISTINCT FROM OLD.concept_id
       AND NEW.value IS NOT DISTINCT FROM OLD.value
       AND NEW.unit IS NOT DISTINCT FROM OLD.unit
       AND NEW.reference_low IS NOT DISTINCT FROM OLD.reference_low
       AND NEW.reference_high IS NOT DISTINCT FROM OLD.reference_high
       AND NEW.measured_at IS NOT DISTINCT FROM OLD.measured_at THEN
        RETURN NEW;  -- a Concept merge re-pointing the reading's hub
    END IF;
    RAISE EXCEPTION 'markers are append-only dated snapshots (issue #16): % blocked', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS markers_no_mutate ON markers;
CREATE TRIGGER markers_no_mutate
    BEFORE UPDATE OR DELETE ON markers
    FOR EACH ROW EXECUTE FUNCTION markers_immutable();

-- A Decision: the owner's time-bound adoption of an intervention (CONTEXT.md
-- "Decision") — the only entity carrying the personal "why". It implements a
-- Protocol (sometimes only partially), so it holds its *own actual* parameters
-- (the owner's dose/timing/frequency/duration) distinct from the Protocol's, which
-- makes deviation from the Protocol first-class. The structural links are edges:
-- `decision -> protocol implements`, `decision -> goal serves`,
-- `decision -> marker motivated_by`, plus `decision -> concept references` (so the
-- Concept-overlap suggester can find relevant Protocols/Claims/Goals) and inbound
-- `claim -> decision supports` for confirmed supporting evidence. No structure
-- CHECK (unlike Protocols): a Decision may be adopted before its parameters are
-- pinned down.
CREATE TABLE IF NOT EXISTS decisions (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action     TEXT        NOT NULL,                       -- the intervention adopted
    dose       TEXT,
    timing     TEXT,
    frequency  TEXT,
    duration   TEXT,
    started_at TIMESTAMPTZ NOT NULL,                       -- when the owner adopted it
    ended_at   TIMESTAMPTZ,                                -- when stopped, if it has
    note       TEXT,                                       -- the owner's rationale prose
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ===========================================================================
-- Slice 13: the Impact engine — change detection (issue #18, PRD #12).
--
-- An Impact is a detected, stance-typed link between newly-arrived knowledge and
-- an existing owner anchor (CONTEXT.md "Impact"), fired in *either direction*: a
-- newly-admitted Claim/Protocol checked against the existing anchors (Decisions,
-- Goals, Markers), or a newly-recorded anchor (a Decision/Goal) checked against the
-- existing Body of Knowledge. Candidate pairs are generated by shared-Concept
-- overlap (the same Concept-traversal machinery query reuses, ADR-0008, ADR-0011)
-- and a `StanceJudge` (an LLM pass) assigns the Stance — so the owner sees genuine
-- change, not merely-related noise; `unrelated` judgements raise nothing.
--
-- Per ADR-0008 an Impact is a *typed entity*, not an edge: it carries a stance and
-- a lifecycle and *points at* its anchor and source by a polymorphic ref. The
-- `source` is always the Claim/Protocol that triggered the finding; the `anchor` is
-- the owner anchor it bears on. The unique constraint dedups on
-- (anchor, source, stance) so re-runs and overlapping evidence never nag twice, and
-- a resolved Impact's surviving row keeps it from being re-raised.
CREATE TABLE IF NOT EXISTS impacts (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- the newly-arrived knowledge that triggered the finding (always a Claim or
    -- Protocol) — polymorphic, so no FK; integrity is kept by deleting an entity's
    -- Impacts when it is deleted (the typed-node delete paths), as edges do.
    source_type TEXT        NOT NULL CHECK (source_type IN ('claim', 'protocol')),
    source_id   BIGINT      NOT NULL,
    -- the existing owner anchor it bears on (a Decision, Goal, or Marker reading).
    -- A superseded supporting Claim (ADR-0005) surfaces as an Impact on the
    -- affected Decision, never silently breaking the link.
    anchor_type TEXT        NOT NULL CHECK (anchor_type IN ('decision', 'goal', 'marker')),
    anchor_id   BIGINT      NOT NULL,
    stance      TEXT        NOT NULL
                            CHECK (stance IN ('reinforces', 'contradicts',
                                              'refines', 'opportunity')),
    -- The lifecycle (CONTEXT.md "Impact"): an Impact never re-nags once resolved.
    --   new → reviewed → actioned | dismissed   (dismiss is reachable from new too,
    --   so a burst can be bulk-dismissed without first reviewing each one).
    state       TEXT        NOT NULL DEFAULT 'new'
                            CHECK (state IN ('new', 'reviewed', 'actioned', 'dismissed')),
    detail      TEXT,                                       -- optional "why" (e.g. supersede)
    -- Actioning an Impact jumps to revising/creating the relevant Decision and
    -- records the resulting link here (CONTEXT.md "Impact"): change detection
    -- driving auditable change. SET NULL if that Decision is later deleted.
    actioned_decision_id BIGINT REFERENCES decisions (id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (anchor_type, anchor_id, source_type, source_id, stance)
);
CREATE INDEX IF NOT EXISTS impacts_inbox ON impacts (state, stance);
CREATE INDEX IF NOT EXISTS impacts_by_anchor ON impacts (anchor_type, anchor_id);


-- ===========================================================================
-- ADR-0013: Concepts connect to each other.
--
-- Two distinct families of Concept->Concept link, stored differently:
--   * Lateral relationships — claim-grounded, derived, self-healing — in the
--     typed `concept_relations` table below, with an evidence link back to the
--     Claims that assert them (NOT the polymorphic `edges` table, whose `kind`
--     CHECK would balloon under the predicate set).
--   * Hierarchy (`broader-of`) — an owner-curated taxonomic edge — is a single new
--     `kind` in the existing `edges` table (added in the `edges` CHECK above when
--     slice 3 lands).
-- Plus an owner-set trust-tier on `creators`, so relationship Strength can weight
-- trusted sources more (ADR-0013 "Strength").
-- ===========================================================================

-- Owner-set trust-tier on a Creator (ADR-0013 "Strength"): higher = more trusted,
-- so a relationship's Strength weights its distinct creators by how much the owner
-- trusts each. Defaults to 1 so an untiered Creator counts as a plain distinct
-- creator — Strength is a distinct-creator count until the owner tiers anyone
-- (user story 29). Idempotent add for a database created before ADR-0013.
ALTER TABLE creators ADD COLUMN IF NOT EXISTS trust_tier INTEGER NOT NULL DEFAULT 1
    CHECK (trust_tier >= 1);

-- A lateral relationship: a directed, typed Concept->Concept link
-- ("APOE4 `risk_factor_for` Alzheimer's"), ADR-0013. It is a *materialized
-- projection of Claims*, not a curated object: derived at admit time from the
-- Claim's predicate triples and recomputed on supersede/delete (ADR-0005). The
-- predicate is CHECK-constrained to the lean, signed, extensible vocabulary
-- (`health_bok.predicates`) — a small lookup, not a Postgres ENUM, consistent with
-- ADR-0008's treatment of edge `kind`; contradiction is *derived* from polarity in
-- code, never stored. The UNIQUE on (src, predicate, dst) makes derivation
-- idempotent: re-admitting the same Claim re-asserts the same relationship.
CREATE TABLE IF NOT EXISTS concept_relations (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    src_concept_id BIGINT NOT NULL REFERENCES concepts (id),
    predicate      TEXT   NOT NULL
                          CHECK (predicate IN (
                              'protects_against', 'risk_factor_for',
                              'increases', 'decreases',
                              'treats', 'worsens',
                              'biomarker_of', 'measured_by', 'mechanism_of',
                              'no_effect_on', 'associated_with')),
    dst_concept_id BIGINT NOT NULL REFERENCES concepts (id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A self-loop is never a meaningful relationship; reject it at the database.
    CHECK (src_concept_id <> dst_concept_id),
    UNIQUE (src_concept_id, predicate, dst_concept_id)
);
CREATE INDEX IF NOT EXISTS concept_relations_by_src ON concept_relations (src_concept_id);
CREATE INDEX IF NOT EXISTS concept_relations_by_dst ON concept_relations (dst_concept_id);

-- The evidence link: which Claim(s) assert each lateral relationship (ADR-0013).
-- This is what makes a relationship claim-grounded and self-healing — its truth
-- comes only from the owner's Claims (ADR-0011). A Claim's deletion cascades its
-- evidence away; the derivation layer then removes any relationship left with no
-- evidence (raising an `eroded` Impact rather than letting it vanish silently).
CREATE TABLE IF NOT EXISTS concept_relation_evidence (
    relation_id BIGINT NOT NULL REFERENCES concept_relations (id) ON DELETE CASCADE,
    claim_id    BIGINT NOT NULL REFERENCES claims (id) ON DELETE CASCADE,
    PRIMARY KEY (relation_id, claim_id)
);
CREATE INDEX IF NOT EXISTS relation_evidence_by_claim
    ON concept_relation_evidence (claim_id);

-- Hierarchy: `broader-of` is a structural, low-cardinality Concept→Concept edge, so
-- it lives in the polymorphic `edges` table as one new `kind` (ADR-0013 amends
-- ADR-0008) — unlike the volatile lateral predicate set, which got its own table.
-- An edge `broader --broader-of--> narrower` is owner-curated taxonomy: it carries
-- `props.confirmed` ('false' = a proposed suggestion, invisible to roll-up;
-- 'true' = the owner confirmed it). Idempotent migration for a database created
-- before ADR-0013 (the inline CHECK above reserved only the five original kinds).
ALTER TABLE edges DROP CONSTRAINT IF EXISTS edges_kind_check;
ALTER TABLE edges ADD CONSTRAINT edges_kind_check
    CHECK (kind IN ('references', 'supports', 'implements', 'serves',
                    'motivated_by', 'broader-of'));

-- Cycle guard: `broader-of` forms a DAG (multi-parent but acyclic), so roll-up and
-- subtree traversal always terminate (user story 17). This BEFORE trigger rejects
-- any `broader-of` edge that would close a loop — a self-loop, or an edge whose
-- narrower endpoint can already reach the broader one by following `broader-of`
-- (in the spirit of ADR-0008's edge-integrity trigger). It considers all
-- `broader-of` edges (proposed or confirmed), so confirming a proposal can never
-- introduce a cycle either.
CREATE OR REPLACE FUNCTION edges_broader_of_acyclic()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.kind <> 'broader-of' THEN
        RETURN NEW;
    END IF;
    IF NEW.src_id = NEW.dst_id THEN
        RAISE EXCEPTION 'broader-of: a Concept cannot be broader of itself (%)', NEW.src_id;
    END IF;
    IF EXISTS (
        WITH RECURSIVE reach(id) AS (
            SELECT NEW.dst_id
          UNION
            SELECT e.dst_id FROM edges e JOIN reach r ON e.src_id = r.id
            WHERE e.kind = 'broader-of'
              AND e.src_type = 'concept' AND e.dst_type = 'concept'
        )
        SELECT 1 FROM reach WHERE id = NEW.src_id
    ) THEN
        RAISE EXCEPTION 'broader-of: edge % -> % would create a hierarchy cycle',
            NEW.src_id, NEW.dst_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS edges_broader_of_no_cycle ON edges;
CREATE TRIGGER edges_broader_of_no_cycle
    BEFORE INSERT OR UPDATE ON edges
    FOR EACH ROW EXECUTE FUNCTION edges_broader_of_acyclic();

-- Relationship-aware alerting (ADR-0013) extends the one Impact inbox rather than
-- adding a second. Impacts gain:
--   * a `relation` source — the lateral relationship that changed (alongside the
--     existing claim/protocol sources);
--   * a `concept` anchor — for the Tier-2 browsable feed, where a notable
--     structural change is anchored on a Concept rather than a Goal/Decision;
--   * two structurally-derived stances, `new_link` (a sign-neutral connection
--     appeared / a backlog summary) and `eroded` (a relationship a Decision relied
--     on lost its last evidence);
--   * a `tier`: Tier-1 (1) is the push inbox, Tier-2 (2) the quieter pull feed.
-- All migrations are idempotent (the CHECKs are dropped and re-added by name).
ALTER TABLE impacts DROP CONSTRAINT IF EXISTS impacts_source_type_check;
ALTER TABLE impacts ADD CONSTRAINT impacts_source_type_check
    CHECK (source_type IN ('claim', 'protocol', 'relation', 'concept'));
ALTER TABLE impacts DROP CONSTRAINT IF EXISTS impacts_anchor_type_check;
ALTER TABLE impacts ADD CONSTRAINT impacts_anchor_type_check
    CHECK (anchor_type IN ('decision', 'goal', 'marker', 'concept'));
ALTER TABLE impacts DROP CONSTRAINT IF EXISTS impacts_stance_check;
ALTER TABLE impacts ADD CONSTRAINT impacts_stance_check
    CHECK (stance IN ('reinforces', 'contradicts', 'refines', 'opportunity',
                      'new_link', 'eroded'));
ALTER TABLE impacts ADD COLUMN IF NOT EXISTS tier INTEGER NOT NULL DEFAULT 1
    CHECK (tier IN (1, 2));
CREATE INDEX IF NOT EXISTS impacts_tier ON impacts (tier, state);

-- One-off backfill bookkeeping (issue #64): re-establishing lateral Relationships
-- across the *pre-existing* library by re-extracting each admitted video's archived
-- Transcript through the supersede path (ADR-0005/0013). The supersede itself is
-- idempotent, so this table exists only to make the batch **resumable** — a row
-- records that a video's re-extraction has completed and committed, so an
-- interrupted run can skip what it already finished and a second full run is a
-- no-op (it never re-pays the Extractor). Keyed by the external video_id like
-- `admissions`/`jobs` (not an FK to `videos`), and never has an entry removed.
CREATE TABLE IF NOT EXISTS relationship_reprocess (
    video_id       TEXT        PRIMARY KEY,
    reprocessed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
