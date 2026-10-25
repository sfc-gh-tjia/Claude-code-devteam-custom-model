#!/usr/bin/env python3
"""Per-profile routing metrics from the gateway's own log (.loop/gateway-routes.jsonl).

    python3 gateway/routes.py                  # every request this gateway has served
    python3 gateway/routes.py --since 2026-10-25 --json

One line per (profile, requested role -> tier): requests, fallbacks, final failures, tokens, cost at
tiers.toml prices, mean latency. This is the gateway's view (every client: standalone sessions and
DevTeam projects alike); `make tokens` is Claude Code's view of one project.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]
from hooks import route_log_path  # noqa: E402
from tiers import load_tiers  # noqa: E402


def summarize(lines: list, tiers: dict, since: str | None = None) -> list:
    by_id = collections.defaultdict(list)
    for r in lines:
        if r.get("id") and r.get("event") in ("route", "served", "failed"):
            by_id[r["id"]].append(r)
    rows = collections.defaultdict(lambda: collections.Counter())
    for recs in by_id.values():
        route = next((r for r in recs if r["event"] == "route"), None)
        if not route or (since and str(route.get("at", "")) < since):
            continue
        done = next((r for r in recs if r["event"] == "served"), None)
        key = (route.get("profile"), route.get("requested"), route.get("tier"))
        c = rows[key]
        c["requests"] += 1
        if not done:
            c["failed"] += 1
            continue
        c["fallbacks"] += bool(done.get("fallback"))
        i, o = int(done.get("input_tokens") or 0), int(done.get("output_tokens") or 0)
        c["input_tokens"] += i
        c["output_tokens"] += o
        price = (tiers.get(done.get("served_tier")) or {}).get("price") or [0, 0, 0]
        c["cost_micro"] += round((i * price[0] + o * price[1]))   # USD per M tokens * tokens = micro-USD
        c["ms"] += round(float(done.get("seconds") or 0) * 1000)
        c["served"] += 1
    out = []
    for (prof, req, tier), c in sorted(rows.items(), key=lambda x: (str(x[0][0]), -x[1]["requests"])):
        out.append({"profile": prof, "role": req, "tier": tier, "requests": c["requests"], "fallbacks": c["fallbacks"],
                    "failed": c["failed"], "input_tokens": c["input_tokens"], "output_tokens": c["output_tokens"],
                    "usd": round(c["cost_micro"] / 1e6, 4), "mean_s": round(c["ms"] / max(c["served"], 1) / 1000, 2)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    f = route_log_path()
    lines = []
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                lines.append(json.loads(line))
            except ValueError:
                pass
    rows = summarize(lines, load_tiers(), a.since)
    if a.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print(f"no routed requests in {f}")
        return 0
    print(f"{'profile':12} {'role':22} {'tier':13} {'req':>5} {'fallb':>5} {'fail':>4} {'in':>10} {'out':>9} {'usd':>8} {'mean_s':>6}")
    for r in rows:
        print(f"{str(r['profile']):12} {str(r['role'])[:22]:22} {str(r['tier']):13} {r['requests']:5} {r['fallbacks']:5} "
              f"{r['failed']:4} {r['input_tokens']:10,} {r['output_tokens']:9,} {r['usd']:8.4f} {r['mean_s']:6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
