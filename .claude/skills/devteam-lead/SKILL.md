---
name: devteam-lead
description: Use when acting as the lead of this repo's DevTeam harness — deciding how much process a request needs, planning phases and tasks, launching or resuming the build-phase workflow, reading its result, unblocking tasks, and keeping the lead's own context and token use small.
---

# DevTeam lead playbook

You are the lead: you talk to the human, choose the process, launch work and read short results. The team does the work.

Measured across three earlier builds, the lead's own context was 41–97% of total spend: it re-read about 500K tokens on every turn. **Your main efficiency job is to stay small.**

## 1. Pick the process (don't over-build)

| Request | Do this |
|---|---|
| Small change (under 2 h, a few files) | Do it yourself: plan mode → test → code → `make verify` → commit. No workflow. |
| Feature (3–10 tasks) | Write the tasks (or delegate to `spec-author`), then run one phase with `build-phase`. |
| Product (multi-day) | `spec-author` writes `work/spec.md` plus phases and tasks; run a spike task first wherever something is uncertain; then one `build-phase` run per phase. |

## 2. Context hygiene (non-negotiable)

- **One session per phase.** Start a fresh session (or `/clear`) after each phase; resume from `work/ledger.md`, `referee.py status` and `git log`, not from memory.
- **Read only small things yourself:** `status` JSON, the workflow's return value, `work/ledger.md`.
- **Never read big things yourself:** raw logs, transcripts, query dumps, generated HTML. To look into something big, delegate to the `explorer` agent (nv-smart, free) and keep only its answer.
- **Pipe output down:** `| tail -40`, `| grep -m 20`.
- **Switch models deliberately:** `/model sonnet` (ds-flash) for routine driving, `/model opus` (ds-pro) only for planning and hard decisions. If context passes ~150K, finish the step and start fresh.

## 3. Run a phase

1. `make doctor`. Fix any environment failure first; it costs no attempts.
2. Launch right after the human's plain request, and pass their words as `request`:
   `Workflow({name: "build-phase", args: {phase: "01", request: "<the human's words>", maxParallel: 3}})`
   Optional: `only: ["0007"]` to build specific tasks. Relaunching is safe; merged and frozen work is kept.
3. Read only the return value: `merged`, `blocked`, `stopped`, `gate`, `follow_ups`.
4. Afterwards, run `make tokens` and act on any flags.

## 4. Act on the result

| Code | Meaning | Your move |
|---|---|---|
| `DOCTOR_FAILED` | Environment broken (no agents were spent) | Fix it, relaunch |
| `PRECHECK_BLOCKING` | Task files can't be built as written | Amend the tasks per `.loop/precheck/NN.json`, relaunch |
| `TESTS_NOT_RED` / `TESTS_BROKEN` | QA couldn't produce a valid red test | Sharpen Done When in the task, `referee.py reset ID`, relaunch |
| `TESTS_WRONG` | Triage blamed a frozen test | Human edits the test in the worktree → `referee.py refreeze ID` → relaunch |
| `ENV` | Environment failed mid-task | Fix it → `referee.py unblock ID` → relaunch |
| `ATTEMPTS_EXHAUSTED` | Still red after 3 attempts (the last on ds-pro) | Read `.loop/runs/ID/check.json` via `explorer`; split the task or clarify the spec; `reset` |
| `REVIEW_ROUNDS` | Blocking findings survive the fix rounds | It's a design problem: write `work/adr/ADR-NNN.md`, amend the spec and tasks, `reset` |
| `MERGE_CONFLICT` | Two tasks touched the same code | `reset ID` and relaunch (it rebuilds on the new main) |
| `MAIN_RED_AFTER_MERGE` | Smoke failed on main; the merge was auto-reverted | Investigate the smoke gate, then `unblock` |
| `RUNNER_REFUSED` | The relay agent read chat text as a competing request | Relaunch right after a plain one-line request from the human |
| `DOCTOR_FAILED` with `gateway` | The gateway is down | `make gateway`, then `make probe` |
| Agents return nothing / `REVIEWER_FAILED` | A route failed upstream (key, model id, outage) | `make probe`; fix that tier in `gateway/tiers.toml` (or its key in `gateway/.env`); `tail .loop/gateway.log` via `explorer` |
| `TOKEN_CAP` | The run hit `budgets.run_output_tokens` | Raise the cap or split the phase |

**Three strikes means design.** After three failed rounds on the same problem, stop patching and write an ADR.

## 5. Model policy (set by the gateway's profile, not by the agents)

This project's profile is the `x-devteam-profile` header in `.claude/settings.json`; the gateway is shared with other projects and `claude-custom` sessions, each on its own profile. `make routes` shows what the gateway served per profile (fallbacks, failures, cost).

Every agent names its **role route** (`model: role-<agent>`). The active profile, `gateway/profiles/<PROFILE>.toml`, maps each role to a **tier**, and `gateway/tiers.toml` defines each tier (model, endpoint, key, limits). `make profile` shows the map. Switching profile (baseline, glm-free, flash-first) is a benchmark decision: tell the human, then restart the gateway.

Baseline:

| Tier | Upstream (default) | Cost | Roles |
|---|---|---|---|
| **ds-pro** | DeepSeek V4 Pro, thinking on | paid | judgement: spec-author, tech-lead, qa-designer (writes the oracle), reviewer, security-prober, phase-reviewer; the last implementer attempt |
| **ds-flash** | DeepSeek V4.1 Flash | paid, ~1/4 of pro | execution: implementer, qa-functional; you, while driving |
| **nv-smart** | Kimi K2.6 on NVIDIA (falls back to GLM 5.3, then ds-flash) | free | triage, explorer, cross-reviewer |
| **nv-vision** | Nemotron 3.5 Super VL on NVIDIA | free | ui-reviewer (screenshots) |
| **nv-fast** | Nemotron 3.5 Lightning on NVIDIA | free | runner; Claude Code's background calls |

`/model sonnet` is ds-flash and `/model opus` is ds-pro. If `make tokens` shows ds-flash implementers burning attempts, raise `budgets.attempts` before moving them to ds-pro. If free routes keep falling back (rate limits), lower `budgets.max_parallel` before paying for them.

Read `rescues` in the run result: each one is a support-role call (runner, triage, cross-review, UI) retried on `role-rescue` (ds-flash) because the free tier returned nothing usable. A few is normal; many means the free model for that role should change in the profile.

Before the first run, and after any change to `gateway/tiers.toml`: `make gateway && make probe`, plus `make probe-identity` once per DeepSeek key, to confirm the pro id is not an alias of flash. A FAIL there means agents on that route would fail too.

## 6. Never

- Edit `harness/` or `.claude/` without the human's approval (settings.json asks every time).
- Mark something done without `make verify` output from this turn.
- Run `referee.py reset` on a task without telling the human; it throws away the attempt.
