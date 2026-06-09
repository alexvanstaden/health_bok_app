# The web Console is the product's primary interface; email is only a notification

> **Terminology & stack (ADR-0009):** "Console" is renamed **Web App**; read every "Console"
> below as "Web App". ADR-0009 records the chosen stack (Python API + Next.js, containerized,
> behind Tailscale), defers interactive graph visualization out of v1 scope, and adds
> natural-language query of the Body of Knowledge in its place.

The owner interacts with the system through a self-hosted web application — the **Console**.
It is the primary surface for the entire system, not an add-on to the pipeline. From the Console
the owner:

- manages **Creators** and triggers **backfill**;
- reviews and approves **Candidates** into the **Body of Knowledge** — both the daily stream and
  the backfill back-catalogue;
- searches, browses, and **visually explores** the Body of Knowledge (Claims, Protocols, Concepts
  and the links between them);
- records and manages the **personal layer** — **Goals**, **Markers**, **Decisions** — and sees how
  evidence connects to it;
- reviews and actions change-detection **Impacts** through their lifecycle.

The daily email **Digest** is demoted to a summary *notification*: it signals that there is new
content and links back into the Console. It is never where curation, exploration, or data entry
happens. The system must remain fully usable with email switched off.

## Why

Earlier framing leaned on email-as-interface — notably "one click to approve from the daily digest"
(ADR-0004). A single notify-plus-action works in an email, but it cannot support browsing a
two-year backfill catalogue, structured personal-layer entry, Impact review with a lifecycle, or
graph exploration. The owner's intent is to *interact with* the knowledge graph, not merely receive
summaries of it. Treating the Console as the product makes that the default rather than a bolt-on.

## Consequences

- **Amends ADR-0004:** approval happens in the Console; the Digest may deep-link to the approval
  queue, but the action resolves in the Console, not the email.
- **Part 2 is not "done" until the Console exists.** A thin Console shell (auth + one real view) is
  foundational early work, not final polish.
- **Reuses the single Postgres (ADR-0003).** The Console reads and writes the same database the
  pipeline does; no second datastore.
- **Single-user, self-hosted.** The Console runs on the same VPS as the cron pipeline, reachable
  only over Tailscale — the tailnet is the auth boundary, no login screen in v1 (ADR-0009); it
  carries no multi-tenant concerns.
- **Graph visualization is a Console concern.** If a graph engine (e.g. Neo4j) is ever introduced,
  it stays a re-derivable projection feeding a view (ADR-0003), never a system of record.
- **Resend stays, smaller.** Email remains for the notification, but its role shrinks; nothing
  essential may depend on it.
