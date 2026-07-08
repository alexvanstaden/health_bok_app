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
Goals, and Markers all *reference* Concepts. Concepts also connect *to each other* two ways
(ADR-0013): a claim-grounded **Relationship** (lateral) and a curated **broader-of** taxonomy
(hierarchy). Unlike Claims, Concepts MAY be merged/normalized — that is their purpose.
_Avoid_: tag, topic, keyword, entity

**Relationship**:
A directed, typed, claim-grounded link between two Concepts — "APOE4 `risk_factor_for`
Alzheimer's" (ADR-0013). Its truth comes *only* from the owner's Claims (no Claim referencing
both Concepts ⇒ no Relationship; lose the last evidencing Claim ⇒ it vanishes), so it is a
*materialized projection* of Claims, never curated by hand. Predicates are signed
(`protects_against ⟷ risk_factor_for`); contradiction is derived from opposite-pairs plus the
rule that `no_effect_on` contradicts any signed predicate on the same pair. **Strength** =
Σ over distinct creators of (owner trust-tier × recency-decay).
_Avoid_: edge, association, link, correlation

**broader-of**:
The *taxonomic* link that rolls narrower Concepts up under a broader one
(Brain metabolism → Brain) — a DAG (multi-parent, acyclic), not claim-grounded, since no creator
asserts it (ADR-0013). The system proposes parents (reusing the issue-#39 suggester) and, under a
**two-tier confidence gate** (ADR-0014), *confirms the confident ones outright* while a looser
proposal stays a suggestion — invisible to roll-up — in the review queue until the owner confirms.
Selecting a Concept shows its **family**: its parents, siblings, and everything under them, with
every Relationship in that family attributed and ranked by Strength.
_Avoid_: parent, category, tag, is-a

**Trust-tier**:
The owner's per-Creator confidence weight (ADR-0013) — the honest "source quality" signal for a
personal system. Feeds Relationship Strength; absent any tiering, every Creator is tier 1 and
Strength is plain distinct-creator count.
_Avoid_: rating, score, rank

## Natural-language Query

The primary way the owner *explores* the Body of Knowledge now that visual graph
exploration is out of v1 scope (ADR-0009, ADR-0011).

**Answer**:
A synthesized prose response to the owner's free-text question, grounded *strictly* in
the owner's own Body of Knowledge (Claims, Protocols) and personal layer (Goals, Markers,
Decisions) — never the model's general medical knowledge, never a blend. An Answer either
cites the specific Claims it rests on or abstains ("nothing in your library covers this");
there is no ungrounded prose. Retrieval reuses the existing pgvector + Concept-traversal
machinery (ADR-0008), so query and the Impact engine share one path.
_Avoid_: chat reply, completion, result, hit

**Citation**:
The link from an Answer back to a specific Claim it rests on — clickable through to that
Claim's Source and locator (the timestamp deep-link for a video). Claims are the unit of
citation; an Answer with no Citation is an abstention, never a guess.
_Avoid_: footnote, reference (a Source is the reference; a Citation points at a Claim)

## Change Detection

**Impact**:
A detected, stance-typed link between newly-arrived knowledge and an existing anchor — fired in
*either direction*: a newly-ingested Claim or Protocol checked against existing anchors (a Decision,
a Claim supporting it, a Goal, or a Marker), **or** a newly-recorded anchor (a Decision or Goal)
checked against the existing Body of Knowledge. Carries a **stance** and is a first-class, persisted
object with a lifecycle (`new → reviewed → actioned | dismissed`), so it never re-nags and leaves an
audit trail of what the owner saw and chose. A new/changed Concept **Relationship** touching a
tracked Concept — or anything in its broader-of subtree — also raises an Impact (ADR-0013): the
**Tier-1** push. Other notable structural changes go to a **Tier-2** browsable feed (pull, gated by
Strength), not the inbox. Widening tracked scope fires a single *summary* Impact for the
pre-existing backlog, then stays quiet.
_Avoid_: alert, flag, notification, match, hit

**Stance**:
The nature of an Impact: `reinforces | contradicts | refines | opportunity` (or `unrelated`,
which is discarded), plus two relationship-native stances (ADR-0013): `new_link` (a sign-neutral
connection appeared) and `eroded` (a Relationship a Decision relied on lost its last evidence).
The evidence-vs-anchor stances come from an LLM pass over Concept-overlap candidates;
relationship stances are derived *structurally* from the graph diff, no LLM pass.
_Avoid_: relation, type, polarity
