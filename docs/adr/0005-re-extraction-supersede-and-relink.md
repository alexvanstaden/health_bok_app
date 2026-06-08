# Re-extraction is automated: supersede within a span, auto-relink, escalate misses as Impacts

ADR-0001 (re-run extraction freely) collides with ADR-0002 (Decisions reference specific
Claims, never merged): naive re-extraction would leave a Decision's provenance dangling.

Resolution: re-extraction **supersedes** the prior Claims from the *same transcript span* —
versioning within a single source, which is *not* the cross-source merging ADR-0002 forbids —
and **automatically re-links** affected `Decision → Claim` edges to the superseding Claim. No
manual re-linking.

When the pipeline cannot confidently match a superseded Claim (the new model dropped or
materially changed it), it does **not** silently break the link. It raises an **Impact** against
the affected Decision — the evidence changed — surfaced in the daily digest like any other
change-detection alert. Superseded Claims are retained (append-only) for audit.
