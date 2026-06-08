# Raw transcript is the source of truth; everything else is re-derivable

The only irreplaceable asset is the raw content of a Source plus its provenance — a video's
transcript, an article's body, a post's text, or pasted research. The upstream item can be
deleted or made private, but the archived raw content lets us regenerate every other artifact
(summary, structured extraction, graph) on demand, against better models or a better schema,
for the price of an LLM call. ("Transcript" is the video-specific case; the principle covers
every Source type — see ADR-0006.)

Therefore v1 archives transcripts immutably and does **not** run structured graph extraction
in the daily pipeline; extraction is deferred and batch-run later over the archive. This keeps
Part 1 fully decoupled from Part 2's still-evolving schema, and makes every downstream artifact
(summaries included) disposable rather than precious.
