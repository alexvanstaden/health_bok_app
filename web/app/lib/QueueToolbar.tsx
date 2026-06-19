"use client";

// The shared filter/search toolbar for both review queues: the daily Review queue
// (`/`) and the Backfill queue (`/backfill`). Established in #75 with the first
// dimension — filter by processing status — and rounded out in #76 with three more:
// filter by Creator, filter by publish-date range, and a free-text search box.
//
// Every dimension narrows the queue server-side and they AND together, so the queue
// is their intersection. Selection is owned by the page (so the 3s poll preserves
// it) via a single `QueueFilters` object the toolbar edits immutably. The optional
// `children` slot carries queue-specific controls the toolbar coexists with — the
// Backfill queue passes its newest/oldest sort there.

import { ReactNode } from "react";
import {
  Creator,
  ProcessingStatus,
  PROCESSING_STATUSES,
  QueueFilters,
} from "./api";

export function QueueToolbar({
  filters,
  onChange,
  creators,
  children,
}: {
  filters: QueueFilters;
  onChange: (next: QueueFilters) => void;
  creators: Creator[];
  children?: ReactNode;
}) {
  const statuses = filters.statuses ?? [];
  const chosenCreators = filters.creators ?? [];

  // Every edit returns a fresh object so the page's state setter sees a new
  // reference and the poll re-subscribes on change.
  function patch(next: Partial<QueueFilters>) {
    onChange({ ...filters, ...next });
  }

  function toggleStatus(status: ProcessingStatus) {
    patch({
      statuses: statuses.includes(status)
        ? statuses.filter((s) => s !== status)
        : [...statuses, status],
    });
  }

  function toggleCreator(channelId: string) {
    patch({
      creators: chosenCreators.includes(channelId)
        ? chosenCreators.filter((c) => c !== channelId)
        : [...chosenCreators, channelId],
    });
  }

  const active =
    statuses.length > 0 ||
    chosenCreators.length > 0 ||
    !!filters.publishedFrom ||
    !!filters.publishedTo ||
    !!filters.search;

  return (
    <div className="toolbar">
      <div className="row toolbar-row">
        <input
          type="search"
          className="search"
          placeholder="Search title, creator, description…"
          value={filters.search ?? ""}
          onChange={(e) => patch({ search: e.target.value })}
        />
        {children && <span className="spacer" />}
        {children}
      </div>

      <div className="row toolbar-row">
        <span className="muted">Status:</span>
        {PROCESSING_STATUSES.map((status) => (
          <label key={status} className="filter-chip">
            <input
              type="checkbox"
              checked={statuses.includes(status)}
              onChange={() => toggleStatus(status)}
            />
            {status}
          </label>
        ))}
      </div>

      {creators.length > 0 && (
        <div className="row toolbar-row">
          <span className="muted">Creator:</span>
          {creators.map((c) => (
            <label key={c.channel_id} className="filter-chip">
              <input
                type="checkbox"
                checked={chosenCreators.includes(c.channel_id)}
                onChange={() => toggleCreator(c.channel_id)}
              />
              {c.name}
            </label>
          ))}
        </div>
      )}

      <div className="row toolbar-row">
        <span className="muted">Published:</span>
        <label className="filter-chip">
          from
          <input
            type="date"
            value={filters.publishedFrom ?? ""}
            onChange={(e) => patch({ publishedFrom: e.target.value || undefined })}
          />
        </label>
        <label className="filter-chip">
          to
          <input
            type="date"
            value={filters.publishedTo ?? ""}
            onChange={(e) => patch({ publishedTo: e.target.value || undefined })}
          />
        </label>
        {active && (
          <button className="link" onClick={() => onChange({})}>
            Clear all
          </button>
        )}
      </div>
    </div>
  );
}
