# Health & Longevity Content Pipeline + Knowledge Graph — Handoff Spec

**Owner:** Alex
**Status:** Ready for grill me phase of brainstorming and design. 
**Purpose:** Hand off to a planning agent for deeper design and implementation.

---

## Original User Request
"Can you help me plan this tech solution:

So there are a few people I follow on X and on YouTube that create content around health and longevity. And this is an area I'm very interested in. So the thing I want you to build for me is I want you to basically check these social media platforms every day and see whether these people have created any new content. And then what I want you to do is I want you to read all the content. So in YouTube's case, get the full transcription. In X's case, just get the full post. And then create a summary out of this and then send it to me. So I can send you the list of all the people I want you to follow and this list will change over time. But that's the first step and I want you to build that now. The second part is I want to start building up a health database for myself where I can store the information you send me plus references to where we got it from and connect it all together with principles, symptoms, supplements, organs, exercise, almost create like a knowledge graph that's personalized to me. And then later, even after that, we'll bring in my own symptoms, bringing my own health record. So for the second part of connecting it all together, let me know what you think about how we could build something like that. So don't build anything yet for the second part. Let's just brainstorm it. So give me some ideas on how we can build it and then how can that evolve over time. Obviously, with both of these, feel free to ask me any questions if you need access to something you don't have access to and you're getting stuck, let me know. I can help you out.

With the knowledge graph part of my request, the mains two goals would be: 1. Be able to link related things in the body of knowledge together and then be able to query that later (like your example). 2. Be able to connect those with my overall health status (which can change over time, blood markers etc) and health goals. 

I want it to be able to connect my health status, my health goals and decisions and  information in the body of knowledge, so that: 1. I always have a connection between my health status and decision and the information that backs that up. 2. If any thing new comes up that is related to any of the existing status, decisions or information supporting a decision then I want it to eb able to identify and let me know that this impacts.

Want to be able to ask questions like:
- What supplements should I be taking for lowering risk of demetia? 
- What I am doing (supplements, excercise, diet) to lower my risk of cardiovascular disease?
- Why am I not allowed to each nitrates?
- Why am I taking a Vitamin D3 supplement?

In all cases the answer needs to include why with references to sources from knowledge base as a priority. Can also include LLM based research if requested, but ideally this would be committed to the knowledge base first.

Feel free to ask me more questions to clarify. We can also break this into different parts."


---

## Project Overview

A personal system for staying on top of health and longevity content, and (later) building a personalized health knowledge graph. Two parts:

1. **Content pipeline:** Monitor specific YouTube creators daily, transcribe new videos, summarize them, and email a digest. Will include other information sources later, such as X/Twitter and email and outputs from LLM deep research.
2. **Knowledge graph:** Store that knowledge, link it together, connect it to personal health status and goals, and surface alerts when new content impacts existing decisions.

---

## Part 1: YouTube Content Pipeline

### Goal
Each day, detect new videos from a list of followed creators, get the full transcript, generate a summary, and email a single digest. The agent reads the transcript and tells Alex what the video covers.

### Decisions Considered so far (can be changed)
- **Scope:** YouTube only. X/Twitter deferred (API now paywalled at ~$200/mo USD).
- **Delivery:** Email, via Resend (free tier, 100 emails/day).
- **Host:** Hostinger VPS, daily cron job.
- **Summarization:** Claude API.
- **Transcript fallback:** OpenAI Whisper API (~$0.006/min) when YouTube captions are unavailable.
- **Storage:** SQLite from day one (forward-compatible with the future graph).
- **Email cadence:** One digest per day, sent only when there is new content.
- **Personalization:** None in v1. Summarize everything. Relevance filtering comes later.

### Pipeline Flow
1. Daily cron (e.g. 6am) runs the job.
2. For each creator, fetch the YouTube RSS feed (`youtube.com/feeds/videos.xml?channel_id=ID`), compare video IDs against already-processed IDs, find new ones.
3. For each new video: pull transcript via `youtube-transcript-api` (timestamped). If no captions, download audio with `yt-dlp` and transcribe via Whisper API.
4. Send transcript to Claude, which returns:
   - A prose summary (goes in the email).
   - Structured JSON (entities, claims, timestamps) stored for the future graph, not emailed in v1.
5. Write a full record to SQLite (provenance + summary + structured extraction).
6. Compile all new summaries into one digest email via Resend. Mark video IDs processed.

### Long Video Handling
For 2+ hour podcasts, chunk the transcript and do map-reduce summarization (summarize sections, then summarize the summaries) so length never breaks the pipeline.

### Backfill (process past videos)
- RSS only returns the latest ~15 videos, so use `yt-dlp --flat-playlist` to list a channel's full back catalogue (no API key, no quota).
- Cost control: cheap first-pass relevance filter on title/description/tags (free, no transcription), then fully process only videos that pass.
- Old videos reliably have captions, so backfill mostly uses the free caption path, not Whisper.
- Run once per creator when added; daily job handles new uploads after that.
- **Open decision:** how far back, and how aggressive the relevance filter (e.g. last 2 years, or top N most relevant).

### Provenance (stored per video)
- Video ID, full URL, title, channel name, channel ID
- Publish date, retrieved date
- Transcript source (YouTube captions vs Whisper)
- Timestamped claims, enabling deep links (`watch?v=ID&t=843s`) back to the exact moment in the video.

### Credentials Needed (env vars on VPS)
- Claude API key
- Resend API key
- OpenAI API key (Whisper fallback)

### Still Needed From Owner
- List of YouTube creators (channel names, @handles, or URLs)
- Destination email address

### Setup Detail
Channels use `@handles` but RSS needs the underlying `channel_id`. Resolve each handle to its channel ID once during setup and store it.

---

## Part 2: Personal Health Knowledge Graph (BRAINSTORM — NOT DECIDED)

### The Real Goal
This is not just a knowledge graph. It is a decision-support system with provenance tracking and change detection. Two core goals:

1. Link related health knowledge together and query it across multiple hops.
2. Connect that knowledge to personal health status (changes over time: blood markers, symptoms) and health goals, so that:
   - There is always a traceable connection between a health status/decision and the evidence behind it.
   - When new content arrives that relates to an existing status, decision, or supporting evidence, the system flags the impact.

### Data Model (four entity types)
- **Body of Knowledge:** claims, principles, mechanisms, protocols, supplement/exercise recommendations. Each traced to a source. Can contradict each other.
- **Health Status:** blood markers, symptoms, biometrics. Time-series; each snapshot dated.
- **Health Goals:** intentions (e.g. improve sleep, reduce inflammation). Stable but can shift or retire.
- **Decisions:** the critical layer (e.g. "400mg magnesium glycinate before bed"). Each links to the goal it serves, the knowledge supporting it, and the status data that motivated it. Time-bound.

### Key Relationships
- Decision SUPPORTED_BY Knowledge claim(s)
- Decision SERVES Goal(s)
- Decision MOTIVATED_BY Health status data point(s)
- Knowledge claim RELATES_TO body system / supplement / symptom / mechanism
- Health status marker INDICATES body system / condition

### Change Detection (both approaches, accepting some early noise)
- **Entity/tag matching:** extract entities from new content, match against entities in the graph. Precise, predictable.
- **Semantic matching:** embed new content, compare against existing decisions/goals/status via vector similarity. Catches conceptual overlaps across differing terminology.
- Decision: use both.

### Representative Queries (validated as covering the need)
Simple lookups (current supplements and why; what a creator said about X; latest marker value). Relationship traversal (evidence behind a decision; decisions related to sleep; has a creator discussed an elevated marker). Multi-hop (contradictions across creators on current protocols; goals with no supporting decisions; everything connected to inflammation). Change detection (new video on Vitamin D + cortisol; does it touch current decisions/goals). Temporal (marker trend over multiple tests; decisions changed in last 6 months and what prompted each).

### Storage Options (DECISION PARKED)
| Concern | Neo4j Only | SQLite Only | Hybrid | SQLite Now, Neo4j Later |
|---|---|---|---|---|
| Graph queries | Excellent | Adequate (degrades at scale) | Excellent | Adequate → Excellent |
| Time-series | Awkward | Excellent | Excellent | Excellent |
| Ops overhead | Medium | Minimal | Higher | Minimal → Medium |
| Backup | Medium | Trivial | Medium | Trivial → Medium |
| Migration risk | None | Migrate later if graph grows | None | Planned migration |

Leading candidate discussed: **hybrid** with clear role separation. SQLite for the health-status timeline and raw ingestion source-of-truth; Neo4j for the relationship graph; the OpenClaw agent in front of both, routing queries and merging results. VPS resources confirmed sufficient for Neo4j (~1-2GB RAM). No final decision made.

### Interaction & Viewing
- **Querying (day-to-day):** OpenClaw agent. Natural language in, agent translates to backend queries. No query language for the owner to learn.
- **Viewing (visual):** Neo4j Browser/Bloom (free, built in if Neo4j is used) for interactive graph exploration; or a custom Next.js dashboard with a force-directed graph lib (react-force-graph / Cytoscape.js); or Obsidian graph view if data also exports to markdown.
- The viewing requirement strengthens the case for Neo4j (capable visualizer out of the box vs building one).

### Future Phases
- Bring in Alex's own health records and symptoms as first-class data.
- Eventually connect personal status/markers directly into the graph for personalized querying ("what supplements have evidence for my specific issues").

---

## Open Decisions to Resolve in Deeper Planning
1. Final storage architecture for Part 2 (Neo4j / SQLite / hybrid / phased).
2. Backfill depth and relevance-filter aggressiveness.
3. Schema design for the four entity types and their relationships.
4. How health status data gets ingested (current tracking method TBD — not yet established with owner).
5. Notification format for change-detection alerts (likely a section in the daily email).

---

## Owner Context (relevant for planning)
- Comfortable with self-hosted infra, VPS management, Docker, Tailscale.
- Has built Next.js dashboards and Supabase/Postgres data pipelines before.
- Already running OpenClaw agents.
- Based in Australia.
