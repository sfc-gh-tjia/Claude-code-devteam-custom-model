# Claude Code on your own models: standalone and DevTeam

Use **Claude Code** with the models you choose, with **no Claude subscription and no Anthropic API key**. There are two ways to use it, and both go through one local gateway:

| Mode | Command | For |
|---|---|---|
| **Standalone** | `claude-custom` in any repo | Everyday coding, debugging and planning: Claude Code as you know it, on your models |
| **DevTeam** | `claude` in a DevTeam-installed repo | Feature work with quality gates: 13 role agents, frozen acceptance tests, a deterministic referee, reviews, and integration with automatic revert |

Out of the box:
- **DeepSeek V4.1 Flash** does the everyday work;
- **DeepSeek V4 Pro** handles hard problems and judgement;
- **free NVIDIA-hosted models** handle support work.

Each model is one block in [`gateway/tiers.toml`](gateway/tiers.toml), so you can swap in any provider LiteLLM supports, or any OpenAI-compatible server (vLLM, Ollama, LM Studio, OpenRouter, Together, your own).

```
 claude-custom (any repo)          claude (DevTeam repo)            claude (another DevTeam repo)
   x-devteam-profile: interactive    x-devteam-profile: baseline      x-devteam-profile: flash-first
            └──────────────────────────────┬──────────────────────────────┘
                                           ▼  Anthropic Messages API
             ONE gateway on 127.0.0.1:4000 (LiteLLM, lives in this template clone)
               per request:  profile (header) → role → tier → model + endpoint + key
               hooks.py:     thinking by effort, drop Anthropic-only fields, strip images for
                             text-only models, cap output, fail closed on unknown roles/profiles
               log:          .loop/gateway-routes.jsonl (profile, role, tier, served model, fallback)
                 ├──▶ DeepSeek API             ds-pro, ds-flash                    (paid)
                 ├──▶ NVIDIA build.nvidia.com  nv-fast, nv-smart, nv-vision, ...   (free tier)
                 └──▶ anything you add         a local vLLM, OpenRouter, ...
```

There is one gateway and one set of keys and models. Each session or project picks its own **profile**, which decides which tier serves each role, so several profiles can run at once.

---

## Setup (once per machine)

### 0. Prerequisites

- **Claude Code** CLI installed (`claude --version`). No Anthropic login is needed.
- **Python 3.11+**, **git**, **make**, **curl**. Node.js is needed only for the workflow-simulation tests.
- Keys for the providers in `gateway/tiers.toml`. For the defaults:
  - **DeepSeek:** <https://platform.deepseek.com/api_keys>, with a small top-up.
  - **NVIDIA (free):** <https://build.nvidia.com>, then "Get API Key".

### 1. Clone the template; it is your gateway's home

```bash
git clone https://github.com/sfc-gh-tjia/Claude-code-devteam-custom-model.git ~/devteam
cd ~/devteam
pip install -r gateway/requirements.txt          # LiteLLM proxy, pinned to the tested version
cp gateway/.env.example gateway/.env             # gitignored: never commit it
$EDITOR gateway/.env                             # DEEPSEEK_API_KEY=…  NVIDIA_API_KEY=…
```

### 2. Check, start, verify

```bash
make harness-test     # 64 tests, no keys or network: harness, gateway, both modes
make gateway          # validates tiers + profiles, starts the gateway in the background on :4000
make probe            # live, per tier: model listed + a real tool-call round trip; all must PASS (< 1 cent)
make probe-identity   # is the advanced tier really a different model from the cheap one? (a few cents)
```

To have the gateway start at login on macOS and restart if it dies, run `make service` (`make service-uninstall` removes it). Elsewhere, run `make gateway` after boot.

---

## Mode 1: standalone Claude Code (`claude-custom`)

```bash
make standalone APPLY=1          # links ~/.local/bin/claude-custom -> this clone; never touches ~/.claude
claude-custom --selfcheck        # PASS = Claude Code answered through the gateway with an empty config, no login
cd ~/any/project && claude-custom
```

At start-up it prints where each kind of request will go:

```
claude-custom · profile interactive · gateway http://127.0.0.1:4000
  /model sonnet (default)  -> ds-flash   deepseek/deepseek-flash            [DeepSeek API (operated from China)]
  /model opus              -> ds-pro     deepseek/deepseek-v4-pro           [DeepSeek API (operated from China)]
  helpers, background      -> nv-smart   nvidia_nim/moonshotai/kimi-k2.6    [NVIDIA free API (trial terms)]
  subagents                -> ds-flash   deepseek/deepseek-flash            [DeepSeek API (operated from China)]
```

- **Switch models** with `/model sonnet` (everyday) and `/model opus` (hard problems). The gateway decides which real model each one is.
- **Plain `claude` is unaffected.** `claude-custom` passes its own settings with `--settings`. It also clears any Anthropic key or endpoint in your shell for that session, so a real key is never sent to the gateway.
- **Options:**
  - `--profile NAME` for a different profile this session (try `thrift` for $0 execution);
  - `--start` to start the gateway if it is down;
  - `--dry-run` to see the settings without launching.
- **Claude Code's own helpers** (the Explore agent, page summaries) use the profile's `background` role. `interactive` gives it Kimi K2.6; that pick is provisional, so compare it with GLM (`nv-glm-flash`) on your own exploration tasks.
- **Inside a DevTeam repo,** `claude-custom` still works, but that repo's guard hooks stay active: settings files add hooks together rather than replacing each other. For unrestricted use, run it in a repo without DevTeam.

## Mode 2: DevTeam (structured feature work)

### Install into a project

```bash
cd ~/devteam
./install.sh ~/code/my-app                 # dry run: lists what would be copied; overwrites nothing
./install.sh ~/code/my-app --apply
```

The project gets:
- the harness (`harness/`);
- the agents, workflow and settings (`.claude/`);
- the plan scaffolding (`work/`);
- a `.devteam-home` file pointing at this clone.

It does **not** get its own gateway: every project uses the shared one, so keys and models are configured once. Use `--with-gateway` only if a project must carry its own copy. If the target already has a `gateway/` from an older install, the installer warns and changes nothing.

### Adapt the project, then run

1. **`harness/project.toml`:**
   - `test_cmd`;
   - `[[setup]]` (for example `uv sync --frozen` or `npm ci`);
   - `[[gates]]` (lint, unit, e2e, cheapest first, each with a timeout);
   - `[lanes]` globs.
2. **`CLAUDE.md`:** fill in the placeholders. Keep it short, because every agent reads it on every run.
3. **`.claude/settings.json`:** this project's profile is `"ANTHROPIC_CUSTOM_HEADERS": "x-devteam-profile: baseline"`. Change it to run this project on `flash-first`, `thrift`, or your own profile.
4. **The plan:** write `work/spec.md` and `work/tasks/NNNN-*.md` from the templates, or ask Claude: *"use spec-author to plan ‹idea›"*.

```bash
cd ~/code/my-app
make doctor        # proves the gateway: DevTeam identity, this repo's token and profile, one tiny request
claude             # then say: run phase 01
```

What happens in a phase:
1. **Preflight**, then the tech-lead checks the plan.
2. **Per task:**
   - the QA designer writes failing tests, and the referee freezes them;
   - the implementer works in its own worktree;
   - the referee checks the gates;
   - the reviewers review: a free cross-family pass first, then the advanced tier decides;
   - the referee integrates: scratch merge, all gates, fast-forward main, smoke test, and automatic revert if main goes red.
3. **Phase gate and phase review.**

Read the result and `work/ledger.md`. Block codes and what to do about each are in `.claude/skills/devteam-lead/SKILL.md`.

### The team (baseline profile)

| Role | Does | Tier → default model |
|---|---|---|
| lead (your session) | Talks to you, launches phases, reads results | `ds-flash` · `ds-pro` on `/model opus` |
| spec-author, tech-lead | Spec and tasks; checks the plan against main | `ds-pro` → DeepSeek V4 Pro |
| qa-designer | Writes the failing acceptance tests (the oracle) | `ds-pro` |
| implementer | Makes the frozen tests pass, in its own worktree | `ds-flash` → V4.1 Flash; last attempt escalates to `ds-pro` |
| reviewer, security-prober, phase-reviewer | Decide what blocks | `ds-pro` |
| qa-functional | Exercises the feature like a user | `ds-flash` |
| cross-reviewer | Lists candidate defects for the reviewer to adjudicate | `nv-smart` → Kimi K2.6 (free) |
| triage, explorer | Classify a failure; read code for the lead | `nv-smart` (free) |
| ui-reviewer | Screenshots against the spec | `nv-vision` → Nemotron 3.5 Super VL (free) |
| runner | Relays one referee command | `nv-fast` → Nemotron 3.5 Lightning (free) |

Models propose and deterministic code decides:
- **The referee** (`harness/referee.py`, plain Python) is the only thing that commits, merges or declares a task done.
- **The guard hook** limits each role to its own paths.
- **Support roles are rescued, judgement roles never downgraded.** If a support role returns unusable output, the call is retried once on a paid tier. A failed reviewer blocks the task instead of silently running on a cheaper model.

---

## Using it without an Anthropic account: what works and what doesn't

**Works.** Everything that runs on your machine:
- editing, Bash, git and tests;
- subagents, skills, workflows, hooks, plan mode and MCP servers.

Verified by `claude-custom --selfcheck` and by the tests `test_claude_custom_works_with_no_anthropic_account` and `test_claude_custom_selfcheck`, which run Claude Code with an empty config directory and no login.

**Doesn't work**, because these run on Anthropic's side:
- **Web search.** Claude Code's built-in WebSearch is executed by Anthropic's API. Add a search MCP server instead, for example `claude mcp add …` with a Tavily or Brave Search server. Fetching a known URL (WebFetch) runs locally and uses the helper model.
- **claude.ai features:** cloud and remote sessions, published artifacts, claude.ai connectors.

**Different:**
- **Quality depends on your models.** Claude Code's prompts and tools are tuned for Claude. DeepSeek and the free models handle everyday work, but may need more retries on hard tasks. DevTeam's gates contain that; in standalone mode, you are the gate.
- **A startup notice is expected.** Claude Code says it doesn't recognize the model ids. That's harmless: the context window is set explicitly to 200K.
- **Terms.** Custom gateways are a supported Claude Code configuration. Check that Claude Code's terms fit your use with non-Anthropic models, especially at work.

---

## Swap or plug in any model

There are three layers, and each change touches one file.

| You want to… | Edit | Then |
|---|---|---|
| use a different **model** for a tier | that tier's `model` / `api_base` in `gateway/tiers.toml` | restart the gateway, `make probe T=<tier>` |
| add a **new model or provider** | a `[tiers.<name>]` block in `gateway/tiers.toml`, plus its key in `gateway/.env` | point a role at it (next row) |
| change which tier does a **job** | that role in `gateway/profiles/<name>.toml` | restart the gateway; `make profile` |
| change one **session or project's** routing | `claude-custom --profile <name>`, or the project's `x-devteam-profile` header | — (no restart) |
| change what an agent **does** | `.claude/agents/<role>.md` (its `model:` stays `role-<role>`) | — |

Restart the gateway with `make gateway-stop gateway`, or with the macOS service: `make service-uninstall service`.

### Worked example: a local model for the implementer

**1. Describe the model** in `gateway/tiers.toml`:

```toml
[tiers.local-coder]
model = "openai/qwen3.5-coder-32b"          # "<LiteLLM provider>/<model id the server expects>"
api_base = "${LOCAL_API_BASE:-http://127.0.0.1:8000/v1}"
api_key_env = ""                             # keyless; for a hosted API name its env var, e.g. "TOGETHER_API_KEY"
thinking = "none"                            # "deepseek" for DeepSeek-style thinking switches, else "none"
think_at = "never"                           # lowest effort that turns thinking on (needs a switch)
vision = false                               # true if it accepts images
max_output = 16384
timeout = 600
fallbacks = ["ds-flash"]                     # tried in order on rate limits / errors
price = [0, 0, 0]                            # input, output, cache-read USD per M tokens
compare_to = "sonnet"                        # the Claude tier it replaces, for the savings estimate
data = "local machine"                       # shown by make profile and claude-custom: where code goes
# extra_body = { chat_template_kwargs = { enable_thinking = false } }   # model-specific switches
```

**2. Give it a job** in a profile, for example a new `gateway/profiles/local.toml`:

```toml
description = "Implementation on the local model; everything else as baseline"
extends = "baseline"

[roles]
implementer = "local-coder"
lead = "local-coder"            # standalone /model sonnet, too
```

**3. Use it:** restart the gateway, then run `make probe T=local-coder`. Then either run `claude-custom --profile local`, or set `x-devteam-profile: local` in a project's settings.

The whole flow is covered by `test_plug_in_any_openai_compatible_model`.

### Provider cheat sheet

| Provider | `model` | `api_base` | Key env var |
|---|---|---|---|
| Any OpenAI-compatible server (vLLM, Ollama, LM Studio, LocalAI, your own) | `openai/<id>` | the server's `/v1` URL | `""` or yours |
| DeepSeek | `deepseek/<id>` (`deepseek-v4-pro`, `deepseek-flash`) | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| NVIDIA build.nvidia.com | `nvidia_nim/<org>/<model>` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |
| OpenRouter | `openrouter/<org>/<model>` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| Others (Together, Fireworks, Groq, Mistral, Gemini, …) | see the [LiteLLM provider list](https://docs.litellm.ai/docs/providers) | per provider | per provider |

Tested here: `openai/`, `deepseek/` and `nvidia_nim/`. Confirm any other provider with `make probe T=<tier>`.

### Rules for choosing models

- **Every model must support tool calling.** `make probe` fails a tier that answers without calling the tool.
- **Give the `ui-reviewer` a tier with `vision = true`.** Other tiers get a note in place of each image.
- **Keep judgement roles on your strongest tier** (qa-designer, reviewer, security-prober, tech-lead) until a benchmark says otherwise. That's what the `flash-first` profile is for.
- **The cross-reviewer is most useful from a different model family** than the implementer.
- **Free tiers need `rpm`** and a paid fallback. Never let a judgement tier fall back silently to a weaker one.

### Profiles shipped

| Profile | Changes from baseline | For |
|---|---|---|
| `baseline` | — (the team table above) | DevTeam projects (the default) |
| `interactive` | Claude Code's helper slot gets Kimi K2.6 instead of the small relay model | `claude-custom` (its default) |
| `flash-first` | Flash also reviews, designs tests and checks plans; Pro only on escalation | testing whether Pro is worth it |
| `glm-free` | One free GLM 5.3 Flash serves all support roles; full GLM 5.3 cross-reviews | testing a simpler free tier |
| `thrift` | Execution on NVIDIA's free copy of DeepSeek V4.1 Flash; only judgement is paid | minimum spend (slower, rate limited) |

`ALLOWED_PROFILES` in `gateway/.env` limits which profiles a client may select (the default is all of them), and an unknown profile is refused. The header is a routing choice on a localhost gateway, not an access control. For a gateway shared by a team, bind allowed profiles to per-user LiteLLM keys instead.

---

## Commands

Run from the template clone or any DevTeam project. Gateway commands follow `.devteam-home` to the shared gateway.

| Command | What it does |
|---|---|
| `make gateway` / `gateway-stop` / `gateway-status` | start (background), stop, check |
| `make service` / `service-uninstall` | macOS: run the gateway at login, restart on crash |
| `make tiers` | validate `tiers.toml` and every selectable profile; render the LiteLLM config |
| `make profile` | role → tier → model → data note (`PROFILE=x make profile` to preview another) |
| `make probe [T=tier]` / `make probe-identity` | live checks with your keys |
| `make routes` | per profile and role: requests, fallbacks, failures, tokens, cost, latency (every client) |
| `make tokens` | this project's spend, context and cache telemetry from Claude Code's transcripts |
| `make standalone APPLY=1` | put `claude-custom` on your PATH |
| `make doctor` / `make verify` | DevTeam preflight / every gate on the current tree |
| `make status` / `ledger` / `reap` | task states / regenerate the ledger / clean up merged worktrees |
| `make harness-test` | the template's own tests (no keys) |

## Layout

```
gateway/      tiers.toml · profiles/ · hooks.py · render.py · tiers.py · probe.py · routes.py · start.sh ·
              service/macos.sh · .env.example · tests/
standalone/   claude-custom (launcher) · settings.json · install.sh
harness/      referee.py · guard.py · gateway_check.py · tokens.py · project.toml · tests/
.claude/      settings.json · agents/*.md (13 roles) · workflows/build-phase.js · skills/devteam-lead/SKILL.md
work/         spec.md · phases/ · tasks/ · adr/ · ledger.md · evidence/
install.sh    install the DevTeam harness into a repo (shared gateway by default)
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `claude-custom`: gateway is not running | The gateway was never started, or was stopped | `make gateway`, or `claude-custom --start`, or `make service` |
| `make gateway`: "set these keys" | A tier names a key that isn't in `gateway/.env` | Add the key, or remove those tiers |
| `make gateway` / `make tiers`: profile error | A profile maps a role to a tier that doesn't exist | The message names it |
| `make doctor`: "not a DevTeam gateway" | Something else is listening on :4000 | Stop it, or set `GATEWAY_PORT` and the project's `ANTHROPIC_BASE_URL` |
| `make doctor`: "rejected this project's token" | The project's `ANTHROPIC_AUTH_TOKEN` differs from `GATEWAY_KEY` | Make them equal |
| `make doctor`: profile "not available" | The project's header names an unknown or disallowed profile | Fix the header, or `ALLOWED_PROFILES` |
| `make probe`: FAIL `tool_use` | The model can't do tool calls reliably | Pick another model for that tier |
| `make probe`: HTTP 400 or 404 | Wrong model id or `api_base` | Compare with the ids `make probe` lists |
| Many `rescues` in a DevTeam result, or fallbacks in `make routes` | A free tier is struggling or rate limited | Move that role in the profile; lower `max_parallel` |
| Startup notice "doesn't recognize the model" | Expected with role routes | Harmless |

## Before you use it on real code

- **Data policy.** Every tier sends your prompts and code to its endpoint, and `claude-custom` and `make profile` show where. With the defaults, DeepSeek's API is operated from China and NVIDIA's free endpoints are under trial terms. Check your organization's policy first. For company code, point the tiers at an approved host or a local server; it's a config change.
- **Verify live.** Routing, translation, profiles, fallbacks and both modes are tested against mocks. Real models are not. Run `make probe`, `make probe-identity` and `claude-custom --selfcheck` with your keys.
- **Quality on cheaper models is a hypothesis.** Pilot a DevTeam phase per profile, then compare attempts, review rounds, blocks and post-merge defects (`work/ledger.md`) and cost (`make routes`, `make tokens`).

## Origin

DevTeam began as a Claude-only harness. The referee, guard, frozen-test oracle, lanes and integrate-with-revert came from three earlier builds:
- the lead's context was 41–97% of spend;
- a relay model corrupted large payloads;
- a gate hung for 10 hours;
- implementers "fixed" the tests;
- a merge turned main red.

This edition adds the model gateway (tiers, per-session profiles, request normalization, routing log), standalone mode, rescue, cross-family review, live probes and cost telemetry for any model mix.
