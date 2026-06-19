"use client";

// The shared filter/search toolbar for both review queues (issue #75): the daily
// Review queue (`/`) and the Backfill queue (`/backfill`). This tracer slice ships
// the first dimension — filter by processing status — and establishes the shape
// every later filter and the search box will slot into.
//
// The status control is a multi-select: each lifecycle state toggles
// independently, selecting one or more narrows the queue, and clearing returns to
// the full list. Selection is owned by the page (so polling preserves it) and the
// page re-fetches server-side on change. The optional `children` slot carries
// queue-specific controls the toolbar coexists with — the Backfill queue passes
// its newest/oldest sort there.

import { ReactNode } from "react";
import { ProcessingStatus, PROCESSING_STATUSES } from "./api";

export function QueueToolbar({
  statuses,
  onChange,
  children,
}: {
  statuses: ProcessingStatus[];
  onChange: (next: ProcessingStatus[]) => void;
  children?: ReactNode;
}) {
  function toggle(status: ProcessingStatus) {
    onChange(
      statuses.includes(status)
        ? statuses.filter((s) => s !== status)
        : [...statuses, status],
    );
  }

  return (
    <div className="row toolbar">
      <span className="muted">Status:</span>
      {PROCESSING_STATUSES.map((status) => (
        <label key={status} className="filter-chip">
          <input
            type="checkbox"
            checked={statuses.includes(status)}
            onChange={() => toggle(status)}
          />
          {status}
        </label>
      ))}
      {statuses.length > 0 && (
        <button className="link" onClick={() => onChange([])}>
          Clear
        </button>
      )}
      {children && <span className="spacer" />}
      {children}
    </div>
  );
}
