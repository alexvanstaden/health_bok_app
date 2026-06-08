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
