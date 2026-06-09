# Extraction is precision-first and auto-admitted; the Body of Knowledge is editable, not gated twice

ADR-0004 gates entry to the Body of Knowledge at the **video grain** and leaves "finer
claim-level curation" to later. This decides what "later" is, and the contract extraction runs to.

## Decision

- **Video-approval is the only human gate.** After approval, the worker extracts Claims and
  Protocols and **auto-admits** them — there is no second review queue.
- **The Body of Knowledge is editable post-hoc.** Every admitted Claim and Protocol can be edited
  or deleted in the Web App, so curation continues *opportunistically in place* — when the owner is
  browsing or reviewing an Impact and spots something wrong, they fix it there. This is how
  ADR-0004's "finer curation later" is realised: by editing, not by a pre-admit gate.
- **Extraction quality bar (the extractor's contract):**
  - **Precision over recall** — extract only the substantive, load-bearing assertions a creator
    actually argues, not every remark, anecdote, or sponsor read.
  - **Preserve scope qualifiers** ("in deficient individuals", "in mice", "at 5g/day") — ADR-0002.
  - **Grounded or dropped** — every Claim carries its locator and is faithful to the transcript;
    anything that can't be grounded is omitted, never smoothed over.
  - **Protocols only when structured** (action + dose/timing/frequency/duration); vague advice
    stays a Claim.

## Considered Options

- **Two-gate (review queue before admit)** — rejected: doubles the review load on every approved
  video and undermines the video-grain trust ADR-0004 deliberately chose.
- **Exhaustive / high-recall extraction** — rejected: micro-claim noise manufactures spurious
  Concept overlaps that wreck Impact detection downstream.

## Consequences

- The extraction prompt must self-censor ungroundable claims.
- The Web App needs edit/delete affordances on Claims and Protocols.
- **Owner edits interact with re-extraction (ADR-0005):** a supersede pass must not silently
  clobber an owner-edited Claim — owner edits are a protected version, not raw extractor output.
- The BoK is deliberately **lean**, not encyclopedic.
