"use client";

// "See Claims": a video's admitted Body-of-Knowledge layer (ADR-0010). Each Claim
// and Protocol shows its locator deep-link back to the exact moment in the source
// video, plus the normalized Concepts it references. Polls while the worker is
// still extracting, so Claims appear as the admission completes.

import { useEffect, useState } from "react";
import { VideoKnowledge, getVideoKnowledge } from "../../../lib/api";

export default function ClaimsView({ params }: { params: { id: string } }) {
  const videoId = params.id;
  const [data, setData] = useState<VideoKnowledge | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const result = await getVideoKnowledge(videoId);
        if (active) {
          setData(result);
          setError(null);
        }
      } catch (e) {
        if (active) setError((e as Error).message);
      }
    }
    load();
    // Keep polling until the admission settles, so Claims show up live.
    const id = setInterval(() => {
      if (!data || ["candidate", "approved", "processing"].includes(data.state)) {
        load();
      }
    }, 3000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [videoId, data]);

  return (
    <>
      <p>
        <a href="/">← Back to the review queue</a>
      </p>
      <h1>Extracted knowledge</h1>
      <p className="subtitle">
        Video <code>{videoId}</code>
        {data && <span className={`badge ${data.state}`}> {data.state}</span>}
      </p>

      {error && <p className="error">API error: {error}</p>}
      {!data && !error && <p className="muted">Loading…</p>}

      {data && data.state !== "admitted" && (
        <p className="muted">
          {data.state === "failed"
            ? "Extraction failed — retry it from the review queue."
            : "Not admitted yet — Claims appear once the worker finishes."}
        </p>
      )}

      {data && data.claims.length > 0 && (
        <>
          <h2>Claims</h2>
          {data.claims.map((claim) => (
            <div key={claim.id} className="claim">
              <div>{claim.text}</div>
              <div className="row">
                <span className="badge">{claim.type}</span>
                <a href={claim.deep_link} target="_blank" rel="noreferrer">
                  ▶ {claim.locator_seconds}s
                </a>
              </div>
              {claim.concepts.length > 0 && (
                <div className="concepts">
                  {claim.concepts.map((c) => (
                    <span key={c} className="concept">
                      {c}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </>
      )}

      {data && data.protocols.length > 0 && (
        <>
          <h2>Protocols</h2>
          {data.protocols.map((p) => (
            <div key={p.id} className="claim">
              <div>
                <strong>{p.action}</strong>
              </div>
              <div className="muted">
                {[
                  p.dose && `dose: ${p.dose}`,
                  p.timing && `timing: ${p.timing}`,
                  p.frequency && `frequency: ${p.frequency}`,
                  p.duration && `duration: ${p.duration}`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
              <div className="row">
                <a href={p.deep_link} target="_blank" rel="noreferrer">
                  ▶ {p.locator_seconds}s
                </a>
              </div>
              {p.concepts.length > 0 && (
                <div className="concepts">
                  {p.concepts.map((c) => (
                    <span key={c} className="concept">
                      {c}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </>
      )}
    </>
  );
}
