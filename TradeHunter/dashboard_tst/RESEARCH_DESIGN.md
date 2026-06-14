# Research page — design

Status: **Phase 1 BUILT** (2026-06-13, dashboard side). Architecture corrected
below after discovering the existing integration is **outbound-only**.

> **ARCHITECTURE CORRECTION (2026-06-13).** The first draft assumed the dashboard
> would relay chat/runs *into* a Runner service on the Nous agent. But the existing
> platform is deliberately **outbound-only** — the agent polls the dashboard
> (`/api/*`, keyed by `TST_INGEST_API_KEY`) and pushes results; the dashboard
> **never reaches into the Linux box** (`routes/agent.py`). So the design changed:
> - **Planning chat → the dashboard calls DeepSeek DIRECTLY** (OpenAI-compatible
>   API; key in the Vault). Real-time, no inbound to the agent. (user choice 2026-06-13)
> - **Runs → outbound-only, like MATP:** the dashboard *queues* a run; the agent
>   polls `/api/research/due`, executes with its corpus, writes md to AI-Hermes, and
>   POSTs the result back. (agent side = Phase 2)
> The Runner-service / inbound-relay design below is **superseded** by this.

A members-facing **Research** page on TradeHunter (`dashboard_tst`) where a user
chats with the Nous-agent LLM to co-design a research topic (macro or
company-specific), the agreed plan is recorded as Markdown, the topic is
scheduled to run on its own cron, and the agent writes the output back into the
Obsidian corpus on AI-Hermes. Over time the linked outputs form a navigable
knowledge graph the agent reads from on each run.

## Decisions locked (user, 2026-06-13)

| # | Decision | Choice |
|---|---|---|
| 1 | LLM provider | **DeepSeek now** (on the Nous agent); Claude pluggable later → provider abstraction, not hard-wired |
| 2 | Network | Hermes (dashboard host) + Nous agent + AI-Hermes are all on the **same LAN** |
| 3 | Scheduling | **Per-topic cron entries** on the Nous agent (`hermes cron`), not a shared dispatcher |
| 4 | Access | **Members** can create/run their own topics (owner-scoped; mods/admin see all) |
| 5 | Corpus | Research Markdown lives on **AI-Hermes** (`MarketResearch/Research/…`), the same share as the EDGAR corpus + Obsidian vault |

## Architecture

```
Browser ──(Cloudflare tunnel, authenticated)──▶ dashboard_tst   (Hermes / Windows)
                                                    │   LAN only (never exposed)
                                                    ▼
                                   Research Runner service (Nous agent / Linux, LAN-only)
                                     • /chat   (stream, DeepSeek now)
                                     • /run    (execute a topic plan -> output md)
                                     • /cron   (add/update/remove a per-topic hermes cron)
                                                    │   cifs /mnt/hermes_sync
                                                    ▼
                              Obsidian corpus on AI-Hermes (//192.168.1.162/MarketResearch)
                              Research/<topic-slug>/PLAN.md  +  runs/<date>.md
                              inputs: EDGAR 10-Q/10-K md, MATP, bars
```

**Security:** the dashboard is the only internet-facing surface (authenticated).
The Runner service is **LAN-only** (bind to the LAN interface / firewall to the
Hermes host) and authenticated with a shared service token. The agent is never
placed behind the tunnel.

**Single writer to the share:** the **Nous agent owns all corpus file I/O**
(it has the cifs mount). The dashboard never writes the share directly — it holds
the DB and pushes content to the Runner, which renders `PLAN.md` and writes run
outputs. This avoids Windows↔cifs write-permission issues and md/DB drift.

## Components

### A. Nous-agent "Research Runner" service (NEW — the main new agent-side piece)
A thin, LAN-only HTTP service wrapping the agent's LLM + corpus + cron:
- `POST /chat` — relay a topic's message thread to the LLM, **stream** tokens back
  (DeepSeek OpenAI-compatible streaming; provider chosen by config). Used by the
  live planning chat.
- `POST /run` — execute a topic's agreed plan against its declared sources, write
  `runs/<date>.md` (+ update a rolling topic summary), emit `[[wikilinks]]`, return
  a summary + path + token/cost.
- `POST /cron` / `DELETE /cron` — create/update/remove the **per-topic `hermes cron`
  entry** whose command is "run topic N". (Honors decision #3.)
- `POST /plan` — render the agreed `PLAN.md` from the transcript+steps the
  dashboard sends.
- Auth: `X-Research-Token` shared secret; bind LAN-only.

*Open question (Q below): if the agent already exposes an HTTP API we extend it;
otherwise this service is the new build. SSH-from-the-web-app is the fallback but
not preferred.*

### B. dashboard_tst additions
- Page `/research` (list of the member's topics + "New topic") and
  `/research/<id>` (the chat + plan + schedule + run history).
- Routes relay chat (SSE) and run/cron actions to the Runner over the LAN.
- Models + Alembic migration (per the data-handling rule: ORM only,
  Postgres-upgradeable).

## Data model (SQLAlchemy ORM, Postgres-ready)

- **ResearchTopic** — id, owner_id, title, `kind` (macro|company), `subject`
  (ticker / theme), `status` (draft|planned|scheduled|running|done|archived),
  `sources` (JSON: edgar/matp/bars/web), `schedule_cron` (str|null),
  `plan_md_path`, `enabled`, created/updated.
- **ResearchMessage** — topic_id, role (user|assistant|system), content, seq,
  created_at. (the planning transcript → rendered into PLAN.md)
- **ResearchRun** — topic_id, started_at, finished_at, `status`
  (queued|running|ok|error), trigger (manual|cron), `output_md_path`, summary,
  tokens, cost, error. (history + cost tracking, shown on the page)

**Source of truth = the DB.** Markdown is a rendered projection the agent writes
to the corpus. PLAN.md (from the transcript+steps) and runs/*.md (from runs) are
separate files so there's exactly one writer per file.

## Corpus layout (AI-Hermes `MarketResearch/Research/`)

```
Research/
  <topic-slug>/
    PLAN.md            # agreed scope + steps + schedule + sources (frontmatter)
    runs/
      2026-06-20.md    # one output per run, datestamped
    <topic-slug>.md    # rolling summary / index note (backlinks to runs)
```
Frontmatter carries topic id, kind, subject, status, schedule, and `[[wikilinks]]`
to related topics — so Obsidian renders the growing graph for free.

## Live chat flow
1. User opens `/research/<id>`, types a message.
2. `POST /research/<id>/chat` persists the user message, relays the thread to the
   Runner `/chat`, streams assistant tokens back to the browser via **SSE**.
3. Assistant message persisted on completion.
4. "Save as plan" → dashboard sends transcript+agreed steps to Runner `/plan` →
   `PLAN.md` written; topic → `planned`.

## Scheduling (per-topic cron — decision #3)
- On "Schedule", the dashboard calls Runner `/cron` with the topic id + cron expr
  → the Runner registers a **per-topic `hermes cron`** entry: `run topic <id>`.
- Editing the schedule updates that entry; archiving/disabling removes it.
- Each fire calls the same `/run` path a manual "Run now" uses → one code path.
- The page shows the next-fire + last-run status (dashboard-visibility rule).

## Run execution — guardrails
- **Per-run deadline + token/cost budget** (mirror the ingest supervisor's
  deadline pattern) so a scheduled run can't run away.
- **Concurrency cap + queue** on the Runner (multiple topics can fire together).
- Company topics pull EDGAR/MATP/bars as context; macro topics pull web + prior
  research. Sources are declared per topic.
- Provider is config-driven (DeepSeek now); a per-topic provider override field is
  reserved for when Claude is added.

## Knowledge graph / RAG (the "network", Phase 3)
Not a trained net — an **emergent linked KB**: every run (a) retrieves related
prior topics/outputs as context and (b) emits `[[wikilinks]]`. Start with linking
(cheap, immediate); add an **embeddings index** over the corpus (EDGAR md seeds
it) once it's big enough to need semantic retrieval.

## Roles / visibility (decision #4)
- Any approved **member** can create, chat, plan, schedule, and run **their own**
  topics. Moderators/admin can see/manage all. Reuse the existing role system.
- Budgets/limits can be tightened per-role later if members' scheduled runs get
  expensive.

## Phased build
- **Phase 1 (MVP):** `/research` page + topic CRUD + **live chat** (SSE relay to
  Runner `/chat`, DeepSeek) + "Save as plan" (`PLAN.md`) + **Run now** (`/run`,
  writes `runs/<date>.md`). No cron yet. + the minimal Runner service.
- **Phase 2:** per-topic `hermes cron` + ResearchRun history/status on the page +
  budgets/deadline + concurrency.
- **Phase 3:** retrieval + auto-linking + (later) embeddings index = the network.

## Open questions to resolve before Phase 1
- **Q1. Agent control surface:** does the Nous agent already expose an HTTP API we
  extend, or do we build the LAN-only "Research Runner" service from scratch?
  (Strongly prefer the service over SSH-from-the-web-app.)
- **Q2. DeepSeek access:** is it a local DeepSeek deployment or the DeepSeek cloud
  API (key)? (Affects streaming + where the key lives — Vault, never the repo.)
- **Q3. Run output style:** what does a "good" run output look like — a structured
  report template (thesis / evidence / risks / sources / action), so outputs are
  consistent and graph-linkable?

## Deploy footprint (when built)
- **Hermes** (`dashboard_tst`): git pull + restart `TST-Dashboard-Web` (+ Alembic
  migration for the new tables).
- **Nous agent**: deploy the Research Runner service (systemd) + the per-topic cron
  command. (Reached via `ssh administrator@192.168.1.163`.)
- Both machine scripts will be given, machine-labeled, per the post-push rule.
```
