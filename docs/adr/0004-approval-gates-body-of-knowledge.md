# Human approval gates all entry into the Body of Knowledge

No content — daily or backfilled — enters the Body of Knowledge automatically. Every video is
a **Candidate** until the owner explicitly approves it (video-level), at which point full
processing runs: transcript fetch (Whisper if needed), Claim/Protocol extraction, Concept
mapping, and change detection.

We deliberately reject auto-ingesting from trusted creators. A *personalized, curated* graph
requires the owner to be the relevance filter; auto-ingestion would fill both the graph and the
change-detection feed with every tangent from every 2-hour podcast. Approval is made
frictionless (one click from the daily digest) so the curation cost stays low. Finer
claim-level curation is possible later; v1 approves at the video grain.
