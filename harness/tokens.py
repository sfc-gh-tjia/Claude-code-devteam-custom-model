#!/usr/bin/env python3
"""Token and cost telemetry for a project, read from Claude Code's own transcripts.

    python3 harness/tokens.py                 # this repo, human-readable
    python3 harness/tokens.py --json          # one JSON object (referee `telemetry` appends it)
    python3 harness/tokens.py --project PATH  # any repo Claude Code has run in
    python3 harness/tokens.py --since 2026-10-20

Sources: ~/.claude/projects/<slug>/<session>.jsonl (the lead), <session>/subagents/*.jsonl
(every subagent and workflow agent, with .meta.json naming its agentType) and
<session>/workflows/wf_*.json (workflow run journals).

Cost is an ESTIMATE at list prices per MTok. Gateway tiers are priced from `price` in
gateway/tiers.toml (DeepSeek at its PEAK list price; off-peak UTC windows are 50% off, so real spend is
usually lower; free tiers at $0). `claude_equiv` prices the same usage on the Claude tier each gateway
tier replaces (`compare_to`) to show the saving.
The ratios are what matter: a lead that re-reads 500K tokens a turn is the waste, whatever the price.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
from pathlib import Path

# Claude list prices (input, output, cache write, cache read) per MTok: v1 tiers and the savings baseline.
PRICE = {"opus": (15, 75, 18.75, 1.5), "sonnet": (3, 15, 3.75, 0.3), "haiku": (1, 5, 1.25, 0.1),
         "fable": (15, 75, 18.75, 1.5)}
# (family, substrings of the model id); first hit wins. Tiers from gateway/tiers.toml are put in
# front by load_gateway(), longest ids first, so a transcript's tier name or upstream id both match.
FAMILY = [("opus", ("opus",)), ("sonnet", ("sonnet",)), ("haiku", ("haiku",)), ("fable", ("fable",))]
CLAUDE_EQUIV: dict = {}   # tier -> the v1 Claude family it replaces (tiers.toml `compare_to`)
FREE: set = set()         # tiers priced at $0
ROLE_TIERS: dict = {}     # role -> tier from the gateway's active profile, for role-* ids in transcripts


def load_gateway(project: Path) -> None:
    """Prices, ids and comparisons from gateway/tiers.toml, and the active profile's role map.
    Without a gateway (a plain v1 repo) only the Claude prices apply."""
    gw = project / "gateway"
    if not (gw / "tiers.py").exists():
        return
    sys.path[:0] = [str(gw)]
    try:
        from tiers import dotenv, load_tiers, model_id
        tiers = load_tiers()
    except Exception as e:  # a broken tiers file: say so, keep going with Claude prices
        print(f"warning: gateway tiers not loaded ({e}); gateway models are unpriced", file=sys.stderr)
        return
    pats = []
    for name, t in tiers.items():
        i, o, c = t["price"]
        PRICE[name] = (i, o, i, c)       # no cache-write surcharge on these providers
        CLAUDE_EQUIV[name] = t["compare_to"]
        if not (i or o or c):
            FREE.add(name)
        pats.append((name, model_id(t).lower()))
    pats.sort(key=lambda x: -len(x[1]))
    FAMILY[:0] = [(n, (n,)) for n in tiers] + [(n, (mid,)) for n, mid in pats]
    try:
        from hooks import load_profile
        name = os.environ.get("PROFILE") or dotenv(gw / ".env").get("PROFILE") or "baseline"
        ROLE_TIERS.update(load_profile(name))
    except Exception:
        pass


TARGETS = {"lead_avg_context": 150_000, "lead_cost_share": 0.20, "output_per_input": 0.01}


def family(model: str | None) -> str | None:
    m = (model or "").lower()
    if m.startswith("role-"):   # priced by the tier the CURRENT profile gives that role
        m = ROLE_TIERS.get(m[5:], m)
    return next((fam for fam, pats in FAMILY if any(p in m for p in pats)), None)


def slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def cost(fam: str, u: dict) -> float:
    p = PRICE[fam]
    return (u.get("input_tokens", 0) * p[0] + u.get("output_tokens", 0) * p[1]
            + u.get("cache_creation_input_tokens", 0) * p[2] + u.get("cache_read_input_tokens", 0) * p[3]) / 1e6


def analyze(root: Path, since: str | None) -> dict:
    agg = collections.defaultdict(collections.Counter)      # (thread, family) -> counters
    roles = collections.defaultdict(collections.Counter)    # agentType -> counters
    big_results = 0
    for f in glob.glob(str(root / "**" / "*.jsonl"), recursive=True):
        thread = "subagent" if "/subagents/" in f else "lead"
        role = None
        if thread == "subagent":
            meta = f[:-6] + ".meta.json"
            try:
                role = json.load(open(meta)).get("agentType")
            except (OSError, ValueError):
                role = None
            role = role or "workflow-agent"
        seen = set()
        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if since and (d.get("timestamp") or "") < since:
                continue
            if thread == "lead" and d.get("type") == "user":
                content = (d.get("message") or {}).get("content")
                if isinstance(content, list):
                    big_results += sum(1 for x in content if isinstance(x, dict) and x.get("type") == "tool_result"
                                       and len(json.dumps(x.get("content"))) > 20_000)
            if d.get("type") != "assistant":
                continue
            m = d.get("message") or {}
            u, fam = m.get("usage") or {}, family(m.get("model"))
            if not u or not fam or m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            c = cost(fam, u)
            for bucket in (agg[(thread, fam)], roles[role] if role else collections.Counter()):
                bucket["turns"] += 1
                bucket["cost_milli"] += round(c * 1000)
                bucket["equiv_milli"] += round(cost(CLAUDE_EQUIV.get(fam, fam), u) * 1000)
                for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                    bucket[k] += u.get(k, 0)
            if role:
                roles[role][f"model_{fam}"] += 1

    def ctx(c):
        return c["input_tokens"] + c["cache_creation_input_tokens"] + c["cache_read_input_tokens"]

    tot, lead = collections.Counter(), collections.Counter()
    for (thread, _), c in agg.items():
        tot.update(c)
        if thread == "lead":
            lead.update(c)
    runs = []
    for f in glob.glob(str(root / "*" / "workflows" / "wf_*.json")):
        try:
            w = json.load(open(f))
        except (OSError, ValueError):
            continue
        if since and (w.get("timestamp") or "") < since:
            continue
        res = (w.get("result") or {}) if isinstance(w.get("result"), dict) else {}
        runs.append({"at": w.get("timestamp"), "name": w.get("workflowName"), "status": w.get("status"),
                     "agents": w.get("agentCount") or 0, "tokens": w.get("totalTokens") or 0,
                     "minutes": round((w.get("durationMs") or 0) / 60000),
                     "merged": len(res.get("merged") or []), "blocked": len(res.get("blocked") or [])})
    cost_total = tot["cost_milli"] / 1000
    summary = {
        "turns": tot["turns"], "output_tokens": tot["output_tokens"], "context_tokens": ctx(tot),
        "est_cost_usd": round(cost_total, 2),
        "claude_equiv_usd": round(tot["equiv_milli"] / 1000, 2),
        "free_turn_share": round(sum(c["turns"] for (_, f), c in agg.items() if f in FREE) / max(tot["turns"], 1), 3),
        "lead_turns": lead["turns"],
        "lead_avg_context": ctx(lead) // max(lead["turns"], 1),
        "lead_cost_share": round(lead["cost_milli"] / max(tot["cost_milli"], 1), 3),
        "output_per_input": round(tot["output_tokens"] / max(ctx(tot), 1), 4),
        "lead_tool_results_over_20k_chars": big_results,
        "workflow_runs": len(runs), "workflow_agents": sum(r["agents"] for r in runs),
    }
    flags = []
    if summary["lead_avg_context"] > TARGETS["lead_avg_context"]:
        flags.append(f"lead re-reads {summary['lead_avg_context']:,} tokens/turn (target < 150K): "
                     "start a new session per phase and read summaries, not logs")
    if summary["lead_cost_share"] > TARGETS["lead_cost_share"] and summary["workflow_agents"]:
        flags.append(f"lead is {summary['lead_cost_share']:.0%} of spend (target < 20%)")
    if tot["turns"] and summary["output_per_input"] < TARGETS["output_per_input"]:
        flags.append(f"output/input {summary['output_per_input']:.2%} (target > 1%): context is being re-read, not used")
    if big_results:
        flags.append(f"{big_results} tool results over 20K chars landed in the lead: delegate or tail them")
    by_model = {f"{t}/{m}": {"turns": c["turns"], "out": c["output_tokens"], "ctx": ctx(c),
                             "cache_hit": round(c["cache_read_input_tokens"] / max(ctx(c), 1), 3),
                             "usd": round(c["cost_milli"] / 1000, 2)} for (t, m), c in sorted(agg.items())}
    by_role = {r: {"turns": c["turns"], "out": c["output_tokens"], "usd": round(c["cost_milli"] / 1000, 2),
                   "models": sorted(k[6:] for k in c if k.startswith("model_"))}
               for r, c in sorted(roles.items(), key=lambda x: -x[1]["cost_milli"])}
    return {"transcripts": str(root), "summary": summary, "flags": flags, "by_model": by_model,
            "by_role": by_role, "runs": sorted(runs, key=lambda r: r["at"] or "")[-20:]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--transcripts", help="transcript dir (default: ~/.claude/projects/<slug of --project>)")
    ap.add_argument("--since", help="ISO date/time; ignore older events")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    load_gateway(Path(a.project))
    root = Path(a.transcripts or os.path.expanduser(f"~/.claude/projects/{slug(a.project)}"))
    if not root.exists():
        print(json.dumps({"summary": None, "flags": [f"no transcripts at {root}"]}) if a.json else f"no transcripts at {root}")
        return 0
    r = analyze(root, a.since)
    if a.json:
        print(json.dumps(r))
        return 0
    s = r["summary"]
    print(f"transcripts: {r['transcripts']}")
    print(f"estimated cost ${s['est_cost_usd']:,.2f} · turns {s['turns']:,} · output {s['output_tokens']:,} · "
          f"context read {s['context_tokens']:,}")
    print(f"same usage on v1 Claude tiers ≈ ${s['claude_equiv_usd']:,.2f} · free-route turns {s['free_turn_share']:.0%}")
    print(f"lead: {s['lead_turns']:,} turns, avg context {s['lead_avg_context']:,}/turn, "
          f"{s['lead_cost_share']:.0%} of cost · output/input {s['output_per_input']:.2%}")
    print("\nby thread/model:")
    for k, v in r["by_model"].items():
        print(f"  {k:18} turns={v['turns']:6,} out={v['out']:>10,} ctx={v['ctx']:>13,} hit={v['cache_hit']:.0%} ${v['usd']:,.2f}")
    print("\nby role:")
    for k, v in list(r["by_role"].items())[:15]:
        print(f"  {k:22} turns={v['turns']:6,} out={v['out']:>9,} ${v['usd']:8,.2f} {','.join(v['models'])}")
    if r["runs"]:
        print("\nrecent workflow runs:")
        for w in r["runs"][-8:]:
            print(f"  {w['at'][:16] if w['at'] else '?'} {w['name']:<16} {w['status']:<9} agents={w['agents']:<4} "
                  f"tokens={w['tokens']:>10,} {w['minutes']}min merged={w['merged']} blocked={w['blocked']}")
    print("\nflags:" if r["flags"] else "\nflags: none — within targets")
    for f in r["flags"]:
        print(f"  ! {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
