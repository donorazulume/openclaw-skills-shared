# openclaw-skills-shared

Shared skill packages used by all three OpenClaw principal agents:
[Roho](https://github.com/donorazulume/openclaw-roho),
[Amara](https://github.com/donorazulume/openclaw-amara), and
[Rob](https://github.com/donorazulume/openclaw-rob).

This repository is consumed as a **git submodule** by each agent repository.
Changes here propagate to agents when each repo updates its submodule reference.

## Skills (canonical per-agent assignment — Issue #281 Batch C)

This table is the **canonical authority** for which shared skills each agent
includes in its image. Each agent's `Dockerfile` MUST mirror this matrix
exactly via selective `COPY ./skills-shared/<skill>/ ...` — _wholesale_
`COPY ./skills-shared/ ...` is forbidden because it leaks Roho-only skills
into Amara/Rob and creates identity-bleed risk.

| Skill | Roho | Amara | Rob | Purpose |
|-------|:----:|:-----:|:---:|---------|
| `lib/`               | ✓ | ✓ | ✓ | Shared Python utility library imported by multiple skills |
| `mattermost-bridge/` | ✓ | ✓ | ✓ | Post envelopes / messages to Mattermost (the inter-agent coordination bus) |
| `rag-brain-manager/` | ✓ | ✓ | ✓ | Open Brain RAG client (hybrid search, ingest, rerank) |
| `openbrain-client/`  | ✓ | ✓ | ✓ | Lower-level Open Brain MCP server client operations |
| `clickup-manager/`   | ✓ | ✓ |   | PARA-board project management integration (Amara: property tickets/maintenance; Roho: full PM) |
| `google-manager/`    | ✓ |   |   | Don's Google Workspace (Calendar, Drive, Contacts) — **Roho only** |
| `gmail-executive/`   | ✓ |   |   | Don's Gmail monitoring, triage, and email management — **Roho only** |

**Why Amara is excluded from `gmail-executive` / `google-manager`:** her
domain is Microsoft 365 (mailbox `amara@chimexhldg.com` via
`chimex-property-manager`, OneDrive via `graph_file_manager`). Don's
Gmail and Google Workspace are Roho's responsibility; Amara dispatches
via `#coordination` if a property workflow needs them.

**Why Rob carries only the four core shared skills:** his domain is
financial / market analysis (HMRC filings, Firefly III reconciliation,
market intel) — none of which require ClickUp, Gmail, or Google Drive.
Rob's local `skills/` adds `rob-hmrc`, `rob-firefly`, and `rob-analytics`.

## Usage

Agent repositories include this repo as a submodule at `skills-shared/`:

```bash
git submodule add https://github.com/donorazulume/openclaw-skills-shared.git skills-shared
```

At Docker build time, shared skills are copied into the image alongside
agent-specific skills. See each agent repo's `Dockerfile` for details.

## Relationship to the Ecosystem

This repo is part of the [OpenClaw ecosystem](https://github.com/donorazulume/openclaw-docker).
Infrastructure (Docker Compose, Caddy, MCP servers, deploy scripts) lives in
`openclaw-docker`; agent-specific skills and configs live in `openclaw-roho`
and `openclaw-amara`.

**ClickUp orchestrator (SPEC-CUOR-001):** The dedup-first CLI `clickup-orchestrator` ships in
[openclaw-docker](https://github.com/donorazulume/openclaw-docker) (reference, tests, guard scripts)
and [openclaw-roho](https://github.com/donorazulume/openclaw-roho) (production image). It is
stdlib-only and is **not** part of this submodule; shared ClickUp HTTP helpers could land in `lib/`
here in the future if needed.

```
openclaw-docker          (infrastructure + base image)
openclaw-skills-shared   (this repo — shared skill packages)
openclaw-roho            (Roho agent skills, config, deploy)
openclaw-amara           (Amara agent skills, config, deploy)
openclaw-rob             (Rob agent skills, config, deploy)
```

All repositories conform to [SPEC-NETARCH-001](https://github.com/donorazulume/openclaw-docker/tree/main/sdds/SPEC-NETARCH-001).
