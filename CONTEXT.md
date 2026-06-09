# Health & Longevity Knowledge System

A personal system that monitors health & longevity content creators, archives and
summarizes their material, and links it into a personalized knowledge graph connecting
evidence to the owner's health decisions, markers, and goals. The owner works with the
system through a self-hosted **Web App**; the daily email **Digest** is only a notification.

## Language

**Web App**:
The self-hosted Next.js web application the owner works in — the primary interface to the whole
system, and the only committed one (ADR-0009). From it the owner manages Creators, triggers
backfill, reviews and approves Candidates into the Body of Knowledge, searches and queries it in
natural language, and records the personal layer (Goals, Markers, Decisions). Everything the
system can do, it does here; the email Digest is only a notification that links back into the Web
App (see ADR-0007, ADR-0009). The system stays fully usable with email switched off. A Python CLI
may exist for ops/admin convenience only — never a primary surface.
_Avoid_: dashboard, admin panel, portal, console

**Source**:
A specific, citable content item that Claims and Protocols are drawn from — a YouTube video, a
web article, an X post, or the owner's pasted LLM-research output — each identified by a
canonical link. Every Source flows through the *same* pipeline into the Body of Knowledge; only
the acquisition step (how its raw content is obtained) differs by type. The owner's separately
conducted research is not special — it is just another Source.
_Avoid_: reference, citation, origin

**Transcript**:
The raw content of a *video* Source — its full verbatim spoken text, and the YouTube-specific
case of a Source's immutable raw content (other types hold article body, post text, or pasted
research instead). The permanent source of truth: retained because the upstream item may vanish,
and every other artifact re-derives from it.
_Avoid_: captions (captions are one possible *source* of a transcript, not the transcript itself)

**Summary**:
A prose write-up of a single transcript, generated for the daily email. A disposable,
re-derivable artifact — never treated as source of truth.
_Avoid_: notes, write-up

**Digest**:
A summary *notification* email — sent only on days with new content — that nudges the owner and
links into the Web App, where the real work (reviewing summaries, approving Candidates, acting on
Impacts) happens. A convenience, never the place curation occurs; the owner can do everything the
Digest references, and far more, directly in the Web App (see ADR-0007).
_Avoid_: newsletter, report, dashboard

**Candidate**:
Any video not yet admitted to the Body of Knowledge. Daily candidates already have an archived
transcript and an emailed summary; backfill candidates are known only by metadata (thumbnail,
title, description, publish date, URL) until approved. Either way, entry into the Body of Knowledge
requires explicit owner **approval** at the video level — the approval *is* the relevance
filter; the owner curates. One gate for all content, daily and backfill.
_Avoid_: backlog item, pending video, unprocessed video

## Body of Knowledge

The impersonal evidence layer — what sources assert, independent of the owner.

**Claim**:
A single, self-contained, falsifiable assertion attributed to a source, carrying its
own provenance (source + timestamp deep-link). The atomic unit of the Body of Knowledge;
"the Body of Knowledge" is the *collection of Claims*, not a single entity. Sub-kinds
(mechanism, principle, finding) are a `type` attribute, not separate entities.
_Avoid_: fact, insight, finding, statement

**Protocol**:
An impersonal, parameterized recommendation: an intervention with structure
(substance/action, dose, timing, frequency, duration), justified by one or more Claims
and attributed to a source. What a creator *recommends* — not what the owner does.
_Avoid_: regimen, recommendation, stack

## Personal Layer

The owner-specific layer — what the owner does, wants, and measures.

**Decision**:
The owner's time-bound adoption of an intervention. It implements a Protocol (sometimes
only partially), serves a Goal, and is motivated by Markers. The only entity that carries
the personal "why." Distinct from an *architectural* decision (those are recorded as ADRs).
_Avoid_: action, choice, intervention

**Goal**:
A stable personal intention or risk the owner wants to address — equally "improve sleep" or
"lower cardiovascular risk." A *concern* is just a Goal stated as a worry; same entity, no
separate "Concern" type. Served by Decisions; an *unmet* Goal — one with no serving Decision —
is the prime target for `opportunity` Impacts.
_Avoid_: objective, target, concern

**Marker**:
An objective, quantitative, dated reading the owner records — value + unit + the lab's
reference range — referencing a Concept (apoB, hsCRP, fasting glucose…). Strictly time-series:
every reading is a dated snapshot, never overwritten, and "out of range" is *derived* from the
stored range. Lab markers first; wearable biometrics are the same shape, added later. Motivates
Decisions and anchors Impacts. (Subjective symptoms are deliberately out of scope for now.)
_Avoid_: status, biomarker, vitals, metric, reading

## Connective Tissue

**Concept**:
A normalized, deduplicated hub node for something the domain talks about — a supplement,
body system, symptom, mechanism, condition, or intervention. Claims, Protocols, Decisions,
Goals, and Markers all *reference* Concepts; relatedness and contradiction are computed by
traversing shared Concepts. Unlike Claims, Concepts MAY be merged/normalized — that is
their purpose.
_Avoid_: tag, topic, keyword, entity

## Change Detection

**Impact**:
A detected, stance-typed link between newly-arrived knowledge and an existing anchor — fired in
*either direction*: a newly-ingested Claim or Protocol checked against existing anchors (a Decision,
a Claim supporting it, a Goal, or a Marker), **or** a newly-recorded anchor (a Decision or Goal)
checked against the existing Body of Knowledge. Carries a **stance** and is a first-class, persisted
object with a lifecycle (`new → reviewed → actioned | dismissed`), so it never re-nags and leaves an
audit trail of what the owner saw and chose.
_Avoid_: alert, flag, notification, match, hit

**Stance**:
The nature of an Impact: `reinforces | contradicts | refines | opportunity` (or `unrelated`,
which is discarded). The judgement comes from an LLM pass over Concept-overlap candidates —
not from Concept overlap alone.
_Avoid_: relation, type, polarity
