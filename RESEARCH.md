# DevTeam v2: research notes (2026-10-25)

How to keep Claude Code as the harness while serving inference from DeepSeek and the NVIDIA free API, which models to use for each role, and what was verified versus what still needs a live run.

## 1. Sources and how far to trust them

The DeepSeek docs host (`api-docs.deepseek.com`, `api.deepseek.com`) refused connections from this machine, and web search was unavailable through the current gateway. So the facts below come from machine-readable sources, cross-checked against each other:

| Source | What it gave |
|---|---|
| `https://integrate.api.nvidia.com/v1/models` (live, public) | The 85 models on NVIDIA's hosted API today |
| `https://huggingface.co/api/models?author=deepseek-ai` | DeepSeek's release timeline |
| `https://openrouter.ai/api/v1/models` and `/models/<id>/endpoints` | Per-provider prices, including DeepSeek's own listing, context and output limits |
| LiteLLM `model_prices_and_context_window.json` (main) | DeepSeek's first-party list prices (sourced from DeepSeek's pricing page), off-peak windows, and vision and reasoning flags |
| `code.claude.com/docs/en/model-config` | How Claude Code handles gateway and custom model ids |
| Local experiments (mock gateway + Claude Code 2.1.310 + LiteLLM 1.80.16) | What Claude Code actually sends, and what LiteLLM actually forwards |

**Confirm with `make probe` once you have keys.** It lists the model ids your DeepSeek key can see, and it runs a tool loop on every route.

## 2. DeepSeek: current models

Releases (Hugging Face): V4-Flash-0731, V4-Pro-0813 (the GA release of V4 Pro), V4-Flash-Vision-Exp (08-31), V4.1-Flash (09-10) and **V4.1-Flash-1017** (10-17, served by DeepSeek under the `deepseek-flash` API name).

| | **DeepSeek V4 Pro** (advanced) | **DeepSeek V4.1 Flash** (most cost-effective) |
|---|---|---|
| Architecture | MoE, 1.6T total / 49B active | Sparse MoE (the first CED architecture), 8B active on input, 16B on output |
| Context / max output | 1M / 393K | 1M / 393K |
| Tools, thinking | yes, yes (hybrid switch) | yes, yes |
| Vision | no | yes |
| List price per MTok (input miss / cache hit / output) | **$1.32 / $0.044 / $3.96** | **$0.30 / $0.006 / $1.20** |
| Off-peak (50% off) | $0.66 / $0.022 / $1.98 | $0.15 / $0.003 / $0.60 |
| Gateway route | `ds-pro` | `ds-flash` |

**Off-peak windows (UTC):** weekdays 00:00–01:00, 04:00–06:00 and 10:00–24:00, plus all of Saturday and Sunday. That is most of the week, and OpenRouter's "DeepSeek" provider row shows exactly the off-peak prices today (a Sunday).

Two things drive token efficiency here:
- **Prefix caching is automatic.** There is no cache-write charge, and a cache hit is about 1/30 (Pro) to 1/50 (Flash) of a miss. Claude Code re-sends a stable prefix (system prompt, then tools, then history) every turn, so most input tokens in an agent loop are hits. The gateway never adds anything in front of the prompt.
- **Thinking costs output tokens, and the switch is binary:** `{"type": "enabled" | "disabled"}`, with no budget.

## 3. NVIDIA free API (build.nvidia.com): what can do these jobs

A free developer key gives OpenAI-compatible access at `https://integrate.api.nvidia.com/v1`. It is rate limited per key; about 40 requests per minute was the documented figure, so verify yours. Its terms are for prototyping and development, not production.

Agent-capable models in today's catalog, chosen per job:

| Job in the team | Pick | Why | Fallback |
|---|---|---|---|
| Cheap judgement: triage, explorer, cross-reviewer | **moonshotai/kimi-k2.6** (`nv-smart`) | Strongest agentic coder on the free list, and from a different family than DeepSeek (see §5) | z-ai/glm-5.3 (`nv-smart-alt`), then ds-flash |
| Screenshots (ui-reviewer) | **nvidia/nemotron-3.5-super-vl-120b-a12b** (`nv-vision`) | Vision-language model with tool calls | ds-flash (V4.1 Flash takes images) |
| Mechanics: runner relay, Claude Code background calls | **nvidia/nemotron-3.5-lightning-30b-a3b** (`nv-fast`) | 3B active parameters: fast, and its output is tiny | ds-flash |
| Also free and useful | deepseek-ai/deepseek-v4.1-flash, nemotron-3-ultra-550b-a55b, minimax-m3.1-flash, glm-5.3-flash | the `thrift` profile serves execution roles from NVIDIA's free copy of V4.1 Flash (tier `nv-ds-flash`) | — |

**On "most token-efficient" for free routes:** the tokens cost $0, but the free routes still have to be reliable. Every 429 or bad tool call becomes a fallback onto a paid route or a retried agent. So free models get the jobs where a mistake is cheap and caught by something else:
- The runner's output is parsed by the workflow.
- The triage default is `impl`.
- The cross-reviewer is adjudicated by ds-pro.
- The explorer only reads.

## 4. How Claude Code routes to non-Claude models (verified)

Claude Code speaks only the Anthropic Messages API. So `ANTHROPIC_BASE_URL` points at a local LiteLLM proxy, which translates each request to OpenAI chat completions for DeepSeek and NVIDIA.

These behaviours were checked with a mock gateway against Claude Code 2.1.310:

1. **A subagent's `model:` frontmatter is sent verbatim.** `model: ds-flash` reached the gateway as `ds-flash`, so each role can name its route directly.
2. **Aliases resolve through env vars.** `--model opus` and the workflow's `model: 'opus'` resolve through `ANTHROPIC_DEFAULT_OPUS_MODEL`, and so on for sonnet and haiku. Background calls use the haiku slot.
3. **Every request carries Anthropic-only fields:** `thinking: {type: "adaptive"}`, `output_config` (effort) and the beta headers. A non-Claude upstream misreads or rejects these.
4. **Unrecognized ids get a 200K window by default,** and `CLAUDE_CODE_MAX_CONTEXT_TOKENS` sets it explicitly. v2 keeps it at 200K even though DeepSeek has 1M: compaction at 200K is what keeps per-turn re-reads, and therefore input spend, small.

LiteLLM 1.80.16 findings (from its source and the tests):
- `/v1/messages` runs proxy pre-call hooks, so one hook can normalize every request.
- `nvidia_nim/` passes tools, `tool_choice` and `max_completion_tokens`.
- **The DeepSeek thinking switch is dropped** whether it is sent as `thinking` or as `reasoning_effort`. Only `extra_body` reaches DeepSeek, so the hook sends it there, and always explicitly: execution roles must never think by default.

## 5. Keeping quality on a cheaper stack

The harness was already built so that quality comes from structure, not from trusting the model:
- frozen tests are the oracle;
- a deterministic referee owns the gates and git;
- integration merges in a scratch tree and auto-reverts if main goes red;
- the last attempt escalates.

v2 keeps all of that and changes four things:

1. **Spend the strongest model where the decisions are.** ds-pro writes the oracle (qa-designer), reviews, probes security, checks the plan and writes the spec. These are the decisions that bound everything downstream.
2. **Cross-family review, for free.** In v1, implementer and reviewer were both Claude; in v2 both are DeepSeek, so their blind spots are correlated. A free Kimi K2.6 pass (`cross-reviewer`) lists up to 8 candidate defects, and the ds-pro reviewer must confirm or reject each one. Only the ds-pro verdict blocks a task, so a noisy free model costs a few reviewer tokens, never a fix round.
3. **Thinking by effort.** ds-pro thinks at effort medium and above, ds-flash only at effort high (implementer, qa-designer), and the free routes never think.
4. **Fallbacks that degrade cost, not judgement:**
   - A free route on 429 falls back to ds-flash.
   - ds-pro falls back to ds-flash.
   - No paid route ever silently falls back to a free one. DeepSeek's thinking switch would 400 there, and a DeepSeek outage should stop the run rather than downgrade the reviewer.

**What is not proven:** whether DeepSeek V4 Pro matches Opus 5.5 on this harness's review and test-design work. The harness makes the gap measurable rather than hidden:
- per-role attempts and rounds go to `work/ledger.md`;
- `make tokens` shows spend next to the v1-equivalent;
- block codes like `ATTEMPTS_EXHAUSTED` and `REVIEW_ROUNDS` reveal model weakness as a number.

Pilot one phase on a real repo before relying on it.

## 6. Expected cost

The same token volume, priced per MTok at peak list rates:

| Tier | v1 (input / cache read / output) | v2 | Ratio |
|---|---|---|---|
| Judgement | Opus $15 / $1.50 / $75 | ds-pro $1.32 / $0.044 / $3.96 | 11× / 34× / 19× cheaper |
| Execution | Sonnet $3 / $0.30 / $15 | ds-flash $0.30 / $0.006 / $1.20 | 10× / 50× / 12.5× cheaper |
| Mechanics | Haiku $1 / $0.10 / $5 | NVIDIA free | ∞ |

At equal token volume, expect a 90–95% lower bill, and about half that again off-peak. The real saving depends on how many extra attempts and rounds the cheaper models need. `make tokens` reports `claude_equiv_usd` beside the estimate, so the pilot shows the true number.

## 7. Risks to decide before use

- **Data residency and policy.** DeepSeek's first-party API is operated from China. NVIDIA's free tier has trial terms. Sending company source code to either may break your organization's AI and data policy; get approval first. Alternatives:
  - Serve the same DeepSeek open weights from an approved host, by changing `DEEPSEEK_API_BASE` and the model ids.
  - Use NVIDIA NIM self-hosted.
  - Use a managed AI platform your organization already approved, if it serves these models.
- **Free tier limits.** About 40 req/min per key, shared by every free route. `max_parallel = 3` plus `rpm: 35` per route keeps under it, and the fallbacks absorb bursts.
- **The thinking + tool-loop round trip on real DeepSeek.** DeepSeek may require `reasoning_content` to be passed back inside a tool loop. The mock can't prove this, and `make probe` tests exactly that case on ds-pro. If it fails, lower ds-pro's `think_at` threshold in `gateway/hooks.py` so it thinks less, or use the OpenRouter or native-Anthropic DeepSeek endpoint.
- **Model ids drift.** DeepSeek renames aliases (`deepseek-flash` now serves the 1017 snapshot). Ids live only in `gateway/tiers.toml`, and `make probe` lists what your key sees.
- **"Pro" may not be Pro.** An external review reported that DeepSeek's pages conflict on whether `deepseek-v4-pro` is served by the Pro weights or redirected to Flash, and that DeepSeek claims V4.1 Flash beats V4 Pro on some evaluations. I could not verify either claim. `make probe-identity` guards against the redirect: it compares the two ids directly, on a fixed reasoning set under identical settings. The Flash-versus-Pro quality question belongs to the benchmark (the `flash-first` profile).

## 8. Added after review (2026-10-25)

- **Role routes and profiles.** Agents name roles, not tiers. `gateway/profiles/*.toml` maps roles to tiers, so the benchmark configurations (`baseline`, `glm-free`, `flash-first`) are a one-line switch with no agent edits. An unknown role fails closed. A test checks that every role route the harness can send (agents, settings, workflow) is defined in every profile.
- **Rescue on unusable output.** Gateway fallbacks only cover HTTP errors. The workflow now also retries a support role's call once on `role-rescue` when it returns nothing, fails its schema, or (for the runner) returns unparsable output. Judgement roles are never rescued onto a cheaper tier.
- **GLM 5.3 Flash** (`z-ai/glm-5.3-flash`, in NVIDIA's live catalog) is available as the tier `nv-glm-flash`, used by the `glm-free` profile.
