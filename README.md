# Claude Code DevTeam: custom-model edition

A drop-in template that turns **Claude Code** into a disciplined software team: planner, test designer, implementer, reviewers, security prober and referee. **Inference runs on whichever models you choose.**

The default setup:
- **DeepSeek V4 Pro** for judgement;
- **DeepSeek V4.1 Flash** for execution;
- **free NVIDIA-hosted models** for support work.

Each model is one block in [`gateway/tiers.toml`](gateway/tiers.toml), so any provider LiteLLM supports, or any OpenAI-compatible server (vLLM, Ollama, LM Studio, OpenRouter, Together, your own), can be swapped in without touching the agents or the workflow.

```
Claude Code (the harness: lead session + build-phase workflow + 13 role agents)
  agents say:  model: role-reviewer, role-implementer, ...       ── Anthropic Messages API ──┐
                                                                                             ▼
gateway/ (LiteLLM proxy on 127.0.0.1:4000)
  profiles/<PROFILE>.toml   role  ──▶ tier          (which tier does each job)
  tiers.toml                tier  ──▶ model + endpoint + key + limits + price
  hooks.py                  per-tier request policy: thinking by effort, drop Anthropic-only fields,
                            strip images for text-only models, cap output, fail closed on unknown roles
          ├──▶ DeepSeek API              ds-pro, ds-flash                      (paid)
          ├──▶ NVIDIA build.nvidia.com   nv-fast, nv-smart, nv-vision, ...     (free tier)
          └──▶ anything you add          e.g. a local vLLM, OpenRouter, ...
harness/ (no LLM)
  referee.py  owns git, worktrees, frozen tests, gates, merge + auto-revert, the ledger
  guard.py    hook before every tool call: each role may write only its own paths
```

## Why it is built this way

Models propose; deterministic code decides. That separation is what makes cheaper models usable:
- **The referee** (`harness/referee.py`, plain Python) is the only thing that commits, merges or declares a task done.
- **Acceptance tests** are written first, made to fail, then frozen with checksums. They are the oracle, and implementers cannot edit them.
- **Every merge** happens in a scratch tree: re-run all gates, fast-forward main, smoke-test main, and revert automatically if it goes red.
- **The guard hook** enforces role permissions in code rather than in prompts. The control plane (`.claude/`, `harness/`, `gateway/`) is off-limits to agents.
- **Spend scales with risk.** Lanes give docs-only tasks less process and auth/SQL/money tasks a security prober. The strongest model is used only where decisions are made.
- **Agents pass file paths, not content,** so no model retypes large payloads. The lead session reads only short JSON results, which keeps its context, and your bill, small.

On a cheaper model mix it adds:
- **Cross-family review.** A free model from a different family than the implementer lists candidate defects, and the advanced-tier reviewer confirms or rejects each one. Only the advanced verdict can block a task.
- **Rescue.** If a support role (runner, triage, cross-reviewer, ui-reviewer) returns unusable output, the call is retried once on a reliable paid tier. Judgement roles are never silently downgraded; a failed reviewer blocks the task.
- **Thinking by effort.** Reasoning tokens are spent only on tiers and roles where it decides.
- **Prefix-cache friendly.** The gateway never changes the prompt prefix, so DeepSeek's automatic cache keeps hitting.

## The team (baseline profile)

| Role | Does | Tier → default model | Cost |
|---|---|---|---|
| lead (your session) | Talks to you, launches phases, reads results | `ds-flash` (`/model sonnet`) · `ds-pro` (`/model opus`) | paid |
| spec-author, tech-lead | Spec and tasks; checks the plan against main | `ds-pro` → DeepSeek V4 Pro | paid |
| qa-designer | Writes the failing acceptance tests (the oracle) | `ds-pro` | paid |
| implementer | Makes the frozen tests pass, in its own worktree | `ds-flash` → DeepSeek V4.1 Flash; the last attempt escalates to `ds-pro` | paid |
| reviewer, security-prober, phase-reviewer | Decide what blocks | `ds-pro` | paid |
| qa-functional | Exercises the feature like a user | `ds-flash` | paid |
| cross-reviewer | Lists candidate defects for the reviewer to adjudicate | `nv-smart` → Kimi K2.6 (then GLM 5.3, then ds-flash) | free |
| triage, explorer | Classify a failure; read code for the lead | `nv-smart` | free |
| ui-reviewer | Screenshots against the spec | `nv-vision` → Nemotron 3.5 Super VL | free |
| runner | Relays one referee command | `nv-fast` → Nemotron 3.5 Lightning | free |

Prices at the time of writing, per million tokens (input / cache hit / output, peak; off-peak is 50% off):
- DeepSeek V4 Pro: $1.32 / $0.044 / $3.96.
- DeepSeek V4.1 Flash: $0.30 / $0.006 / $1.20.
- NVIDIA free endpoints: $0, rate limited (about 40 requests per minute per key).

The research behind these choices is in [`RESEARCH.md`](RESEARCH.md).

---

## Step-by-step setup

### 0. Prerequisites

- **Claude Code** CLI (`claude --version`). It is the harness; inference goes to your models, not Anthropic's.
- **Python 3.11+**, **git**, **make**, **curl**, and **Node.js** (Node is only needed for the workflow-simulation tests).
- API keys for the providers in `gateway/tiers.toml`. For the defaults:
  - **DeepSeek:** <https://platform.deepseek.com/api_keys>, with a small top-up.
  - **NVIDIA (free):** <https://build.nvidia.com>, then "Get API Key".

### 1. Get the template and install the gateway

```bash
git clone https://github.com/sfc-gh-tjia/Claude-code-devteam-custom-model.git devteam
cd devteam
pip install -r gateway/requirements.txt        # LiteLLM proxy, pinned to the tested version
```

### 2. Add your keys

```bash
cp gateway/.env.example gateway/.env           # gitignored: never commit it
$EDITOR gateway/.env                           # set DEEPSEEK_API_KEY and NVIDIA_API_KEY
```

### 3. Check the harness itself (no keys, no network)

```bash
make harness-test                              # 49 tests: referee, guard, workflow simulation, gateway
```

The gateway tests run the real LiteLLM proxy and real Claude Code against a mock model server. They cover routing, translation, tool calls, streaming, fallbacks, profiles and plugging in a custom model.

### 4. Start the gateway and check every model live

```bash
make gateway          # renders the config from tiers.toml, validates the profile, starts on :4000
make profile          # shows each role → tier → model → where your data goes
make probe            # per tier: model id listed, a real tool-call round trip; must be all PASS (< 1 cent)
make probe-identity   # is the "advanced" tier really a different model from the cheap one? (a few cents)
```

If a tier FAILs, fix it in `gateway/tiers.toml`, then run `make gateway-stop && make gateway && make probe ‹tier›`.

### 5. Install the template into your project

```bash
./install.sh /path/to/your/repo                # dry run: lists what would be copied; overwrites nothing
./install.sh /path/to/your/repo --apply
cd /path/to/your/repo
cp /path/to/devteam/gateway/.env gateway/.env   # or create it from .env.example
```

Then adapt three files:
1. **`harness/project.toml`:**
   - `test_cmd`;
   - `[[setup]]` (for example `uv sync --frozen` or `npm ci`);
   - `[[doctor]]` checks;
   - `[[gates]]` (lint, unit, e2e, cheapest first, each with a timeout);
   - `[lanes]` globs.
2. **`CLAUDE.md`:** fill in the placeholders. Keep it short, because every agent reads it on every run.
3. **`work/spec.md`:** write the spec and its numbered requirements. Write the tasks under `work/tasks/` using `TEMPLATE.md`, or ask Claude: *"use spec-author to plan ‹idea›"*.

```bash
make gateway && make doctor                    # doctor fails fast if the gateway or the toolchain is broken
```

### 6. Run a phase

```bash
claude                                         # in your repo; .claude/settings.json points it at the gateway
```

Say **"run phase 01"**. The lead launches the `build-phase` workflow:
1. preflight;
2. plan check;
3. per task: tests, then implementation, then review, then integration;
4. phase gate and phase review.

Read the result and `work/ledger.md`. Block codes and what to do about each are in `.claude/skills/devteam-lead/SKILL.md`.

### 7. Measure

```bash
make tokens        # spend by role and tier, cache hit rate, lead context, share of free-tier turns,
                   # and what the same usage would have cost on Claude models
```

---

## Swap or plug in any model

There are three layers, and each change touches exactly one file.

| You want to… | Edit | Then |
|---|---|---|
| use a different **model** for an existing tier | that tier's `model` (and `api_base`) in `gateway/tiers.toml` | `make gateway-stop gateway probe` |
| add a **new model or provider** | a new `[tiers.<name>]` block in `gateway/tiers.toml`, plus its key in `gateway/.env` | point a role at it (next row) |
| change which tier does a **job** | that role in `gateway/profiles/<name>.toml` | restart the gateway; `make profile` |
| try a whole different **configuration** | `PROFILE=<name>` in `gateway/.env` | restart the gateway |
| change what an agent **does** | `.claude/agents/<role>.md` (its prompt and tools; its `model:` stays `role-<role>`) | — |

### Add a model: worked example

Say you want the implementer on a local vLLM server.

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
price = [0, 0, 0]                            # input, output, cache-read USD per M tokens (for make tokens)
compare_to = "sonnet"                        # the Claude tier it replaces, for the savings estimate
data = "local machine"                       # shown by make profile: where your code goes
# extra_body = { chat_template_kwargs = { enable_thinking = false } }   # model-specific switches, sent verbatim
```

**2. Give it a job** in a profile, for example a new `gateway/profiles/local.toml`:

```toml
description = "Implementation on the local model; everything else as baseline"
extends = "baseline"

[roles]
implementer = "local-coder"
```

**3. Activate it and check it:**

```bash
echo "PROFILE=local" >> gateway/.env
make gateway-stop gateway
make profile                    # role-implementer -> local-coder -> openai/qwen3.5-coder-32b
make probe local-coder          # must PASS: a real tool-call round trip through the gateway
```

That's it: no agent, workflow or settings changes. This exact flow is covered by `test_plug_in_any_openai_compatible_model`.

### Provider cheat sheet (`model` prefixes)

| Provider | `model` | `api_base` | Key env var |
|---|---|---|---|
| Any OpenAI-compatible server (vLLM, Ollama, LM Studio, LocalAI, your own) | `openai/<id>` | the server's `/v1` URL | `""` or yours |
| DeepSeek | `deepseek/<id>` (e.g. `deepseek-v4-pro`, `deepseek-flash`) | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| NVIDIA build.nvidia.com | `nvidia_nim/<org>/<model>` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |
| OpenRouter | `openrouter/<org>/<model>` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| Together, Fireworks, Groq, Mistral, Gemini, … | see the [LiteLLM provider list](https://docs.litellm.ai/docs/providers) | per provider | per provider |

Tested in this repo: `openai/`, `deepseek/` and `nvidia_nim/`. Other LiteLLM providers should work the same way. Confirm each with `make probe <tier>` before relying on it.

### Rules for choosing models

- **Every model must support tool calling.** Agents work through tools, and `make probe` fails a tier that answers without calling the tool.
- **Give the `ui-reviewer`'s tier `vision = true`.** Other tiers get a short note in place of each image.
- **Keep judgement roles** (qa-designer, reviewer, security-prober, tech-lead) on your strongest tier. Benchmark before moving them; the `flash-first` profile exists for that.
- **The cross-reviewer is most useful from a different model family** than the implementer.
- **Free tiers need `rpm`** and a paid fallback, so a rate limit costs a little money instead of blocking a run.
- **Never fall back from a judgement tier to a weaker one silently.** A failure there should stop and surface.

### Profiles shipped

| Profile | What changes from baseline | Use it to |
|---|---|---|
| `baseline` | — (the table above) | everyday runs |
| `glm-free` | One free GLM 5.3 Flash serves all support roles; full GLM 5.3 cross-reviews | test a simpler free tier |
| `flash-first` | Flash also reviews, designs tests and checks plans; Pro only on escalation | test whether Pro is worth it |
| `thrift` | Execution runs on NVIDIA's free copy of DeepSeek V4.1 Flash; only judgement is paid | minimum spend (slower, rate limited) |

---

## Commands

| Command | What it does |
|---|---|
| `make gateway` / `make gateway-stop` / `make gateway-status` | start (background), stop, or check the gateway |
| `make tiers` | validate `tiers.toml` and the profile, and render the LiteLLM config |
| `make profile` | role → tier → model → data note for the active profile (`PROFILE=x make profile` to preview another) |
| `make probe [tier…]` | live check of tiers with your keys |
| `make probe-identity` | check that the advanced tier is a different model from the cheap one |
| `make doctor` | preflight: gateway, toolchain, ports, credentials |
| `make verify` | every gate on the current tree; "done" means this is green |
| `make status` / `make ledger` / `make reap` | task states; regenerate `work/ledger.md`; clean up merged worktrees and branches |
| `make tokens` | spend and context telemetry from Claude Code's transcripts |
| `make harness-test` | the harness's own tests (no keys) |

## Layout

```
gateway/      tiers.toml · profiles/ · hooks.py · render.py · tiers.py · probe.py · start.sh · .env.example · tests/
harness/      referee.py · guard.py · tokens.py · project.toml · tests/
.claude/      settings.json (points Claude Code at the gateway) · agents/*.md (13 roles) ·
              workflows/build-phase.js · skills/devteam-lead/SKILL.md (the lead's playbook)
work/         spec.md · phases/ · tasks/ · adr/ (your plan) · ledger.md · evidence/ (written by the referee)
CLAUDE.md     rules every agent reads (keep it short)
install.sh    copy the template into a repo without overwriting anything
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `make gateway`: "set these keys" | A tier names a key that isn't in `gateway/.env` | Add the key, or remove the tiers that use it |
| `make gateway`: profile error | A profile maps a role to a tier that doesn't exist | `make tiers` shows which one |
| `make probe`: FAIL `tool_use` | The model can't do tool calls reliably | Pick another model for that tier |
| `make probe`: FAIL HTTP 404 or 400 | Wrong model id or `api_base` | Compare with the ids `make probe` lists for the endpoint |
| Agents return nothing; `REVIEWER_FAILED` | A tier is failing upstream | `make probe`, then `tail -50 .loop/gateway.log` |
| Many `rescues` in a run result | A free tier keeps producing unusable output | Move that role to another tier in the profile |
| `DOCTOR_FAILED` on `gateway` | The gateway isn't running | `make gateway` |
| Claude Code warns about an unrecognized model | Expected: role routes aren't Claude ids | Harmless; the window is set by `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |

## Before you use it on real code

- **Data policy.** Every tier sends your code to its endpoint, and `make profile` shows where each role's data goes. With the defaults, DeepSeek's API is operated from China and NVIDIA's free endpoints are under trial or prototype terms. Check your organization's policy first. To keep code in-house, point the tiers at an approved host or a local server; it's a config change.
- **Verify live before you trust it.** The harness, routing, translation and fallbacks are covered by tests against mocks. Real-model behaviour is not: tool-call reliability, thinking inside tool loops, rate limits. `make probe` and `make probe-identity` check those per key.
- **Quality on cheaper models is a hypothesis until you measure it.** Pilot one phase, then compare attempts, review rounds, blocks and post-merge defects in `work/ledger.md` across profiles. `make tokens` shows the cost side.

## Origin

DevTeam began as a Claude-only harness. The referee, guard, frozen-test oracle, lanes and integrate-with-revert came from lessons on three earlier builds:
- the lead's context was 41–97% of spend;
- a relay model corrupted large payloads;
- a gate hung for 10 hours;
- implementers "fixed" the tests;
- a merge turned main red.

This edition keeps all of that and adds the model gateway: tiers, profiles, the request normalizer, rescue, cross-family review, live probes and cost telemetry for any model mix.
