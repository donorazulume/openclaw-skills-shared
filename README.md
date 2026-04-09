# openclaw-skills-shared

Shared skill packages used by both [Roho](https://github.com/donorazulume/openclaw-roho)
and [Amara](https://github.com/donorazulume/openclaw-amara) OpenClaw agents.

This repository is consumed as a **git submodule** by both agent repositories.
Changes here propagate to agents when each repo updates its submodule reference.

## Skills

| Skill | Purpose |
|-------|---------|
| `lib/` | Shared Python utility library imported by multiple skills |
| `clickup-manager/` | ClickUp project management integration |
| `google-manager/` | Google Workspace (Calendar, Drive, Contacts) integration |
| `gmail-executive/` | Gmail monitoring, triage, and email management |
| `openbrain-client/` | Open Brain MCP server client operations |

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
```

All repositories conform to [SPEC-NETARCH-001](https://github.com/donorazulume/openclaw-docker/tree/main/sdds/SPEC-NETARCH-001).
