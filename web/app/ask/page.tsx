"use client";

// Ask the Body of Knowledge a question (issue #17, ADR-0011). The primary way the
// owner *explores* their library now that a visual graph is out of v1 scope
// (ADR-0009): a free-text question gets a synthesized answer grounded STRICTLY in
// the owner's own Claims, Protocols, and personal layer — never general knowledge.
// Every answer cites the specific Claims it rests on, each clickable through to its
// Source (the locator deep-link) and to the Claim's detail; when nothing covers the
// question the assistant abstains rather than confabulating.

import { useState } from "react";
import { Citation, QueryAnswer, askQuestion } from "../lib/api";

export default function Ask() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<QueryAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await askQuestion(q));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Ask</h1>
      <p className="subtitle">
        Ask your Body of Knowledge a question. Answers are grounded strictly in your
        own library — Claims, Protocols, Goals, Markers, and Decisions — and cite the
        evidence they rest on. Nothing covered? It says so.
      </p>

      <form onSubmit={ask} className="card">
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. What lowers apoB, given my last reading?"
          rows={3}
          style={{ width: "100%" }}
        />
        <div className="row">
          <button className="primary" disabled={busy || !question.trim()}>
            {busy ? "Asking…" : "Ask"}
          </button>
        </div>
      </form>

      {error && <p className="error">API error: {error}</p>}

      {result && !busy && (
        <section className="card">
          {result.abstained ? (
            <p className="muted">{result.answer}</p>
          ) : (
            <>
              <p className="answer">{result.answer}</p>
              <h3>Citations</h3>
              <ol>
                {result.citations.map((c) => (
                  <li key={c.claim_id}>
                    <Cite c={c} />
                  </li>
                ))}
              </ol>
            </>
          )}
        </section>
      )}
    </>
  );
}

function Cite({ c }: { c: Citation }) {
  return (
    <span className="cite">
      <a href={`/claims/${c.claim_id}`}>{c.text}</a>
      <span className="meta row">
        <span className="badge">{c.type}</span>
        <span>· {c.source_title}</span>
        <span>
          ·{" "}
          <a href={c.deep_link} target="_blank" rel="noreferrer">
            watch source ↗
          </a>
        </span>
      </span>
    </span>
  );
}
