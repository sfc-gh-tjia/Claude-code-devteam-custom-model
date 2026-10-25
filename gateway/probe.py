#!/usr/bin/env python3
"""Live check of every tier in gateway/tiers.toml with your real keys (run after `make gateway`).

    python3 gateway/probe.py              # every tier, plus one role route
    python3 gateway/probe.py nv-fast      # one tier (e.g. one you just added)
    python3 gateway/probe.py --identity   # is the [probe].identity pair really two different models?

Per tier:
1. Model id: the tier's id is listed by its endpoint's /models (for OpenAI-compatible endpoints).
2. Through the gateway: a tool call, then the tool result, then a final answer. On a thinking tier
   this runs with thinking on, which is the case a mock cannot prove (e.g. DeepSeek's reasoning in a
   tool loop).
3. A role route (role-runner) resolves through the active profile.
Prints one line per tier and writes details to .loop/probe.json. Costs well under one cent.

--identity (`make probe-identity`, a few cents): asks the two tiers' endpoints directly, so the
gateway can't mask it, 8 fixed reasoning questions with identical settings, then compares reported
model ids, scores and reasoning-length profiles. It catches an "advanced" id that is really an
alias of the cheap one; it does not measure quality (that is the benchmark's job).
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]
from tiers import env_view, load_tiers, model_id, probe_settings  # noqa: E402

TOOL = {"name": "get_build_status", "description": "Return the CI status of a git branch.",
        "input_schema": {"type": "object", "properties": {"branch": {"type": "string"}}, "required": ["branch"]}}


def http(url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                               headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as ex:
        return ex.code, {"error": ex.read().decode(errors="replace")[:400]}
    except (urllib.error.URLError, TimeoutError, ValueError) as ex:
        return 0, {"error": str(ex)[:400]}


def auth(e: dict, t: dict) -> dict:
    k = e.get(t["api_key_env"], "") if t["api_key_env"] else ""
    return {"authorization": f"Bearer {k}"} if k else {}


def listed_ids(e: dict, tiers: dict) -> dict:
    """GET <api_base>/models once per (endpoint, key); None where the endpoint can't list."""
    seen, out = {}, {}
    for name, t in tiers.items():
        if not t["api_base"]:
            out[name] = None
            continue
        key = (t["api_base"], t["api_key_env"])
        if key not in seen:
            code, d = http(t["api_base"].rstrip("/") + "/models", headers=auth(e, t), timeout=30)
            seen[key] = sorted(m["id"] for m in d.get("data", [])) if code == 200 and isinstance(d.get("data"), list) else None
        out[name] = seen[key]
    return out


def tool_loop(base, key, route, effort):
    h = {"x-api-key": key, "authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"}
    msgs = [{"role": "user", "content": "Use the tool to get the CI status of branch main, then answer in one short sentence."}]
    body = {"model": route, "max_tokens": 2048, "tools": [TOOL], "messages": msgs,
            "thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    t0 = time.time()
    code, r1 = http(base + "/v1/messages", body, h, timeout=300)
    if code != 200:
        return {"ok": False, "step": "first call", "http": code, "error": r1.get("error")}
    tu = [b for b in r1.get("content", []) if b.get("type") == "tool_use"]
    if not tu:
        return {"ok": False, "step": "tool_use", "error": "model answered without calling the tool",
                "content": json.dumps(r1.get("content"))[:300]}
    msgs += [{"role": "assistant", "content": r1["content"]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu[0]["id"], "content": "green: 412 passed"}]}]
    code, r2 = http(base + "/v1/messages", {**body, "messages": msgs}, h, timeout=300)
    if code != 200:
        return {"ok": False, "step": "after tool_result", "http": code, "error": r2.get("error")}
    text = " ".join(b.get("text", "") for b in r2.get("content", []) if b.get("type") == "text").strip()
    return {"ok": bool(text), "seconds": round(time.time() - t0, 1), "answer": text[:160],
            "thinking_blocks": sum(b.get("type") == "thinking" for b in r1["content"] + r2.get("content", [])),
            "usage": [r1.get("usage"), r2.get("usage")], "served_by": r2.get("model")}


# Fixed reasoning set with exact integer answers, for the identity check. Easy enough that both tiers
# should get most right; the point is to compare the two models' fingerprints, not to rank them.
QUIZ = [
    ("How many positive integers n <= 1000 are divisible by 3 or by 5, but not by 15?", 401),
    ("What are the last two digits of 7^2026? Give them as an integer.", 49),
    ("In Python, what does sum(v * i for i, v in enumerate([1, 2, 3, 4, 5][::-1])) return?", 20),
    ("In how many ways can a 2x10 board be tiled with 1x2 dominoes?", 89),
    ("What is the smallest positive integer that leaves remainder 1 when divided by each of 2, 3, 4, 5 and 6, "
     "and is divisible by 7?", 301),
    ("How many trailing zeros does 125! have?", 31),
    ("In Python, what does len({i % 7 for i in range(0, 100, 3)}) return?", 7),
    ("How many monotone lattice paths from (0,0) to (6,6) with unit steps right or up never go above the line y = x?", 132),
]


def last_int(text: str):
    nums = re.findall(r"-?\d+", (text or "").replace(",", ""))
    return int(nums[-1]) if nums else None


def ask_direct(e: dict, t: dict, question: str) -> dict:
    """One chat completion straight to the tier's endpoint (not through the gateway), thinking on if it can."""
    body = {"model": model_id(t), "max_tokens": 8000,
            "messages": [{"role": "user", "content": question + " Reply with the final integer alone on the last line."}]}
    if t["thinking"] == "deepseek":
        body["thinking"] = {"type": "enabled"}
    t0 = time.time()
    code, d = http(t["api_base"].rstrip("/") + "/chat/completions", body, auth(e, t), timeout=300)
    if code != 200:
        return {"error": f"HTTP {code}: {str(d.get('error'))[:160]}"}
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    u = d.get("usage") or {}
    return {"model": d.get("model"), "fingerprint": d.get("system_fingerprint"), "answer": last_int(msg.get("content")),
            "out_tokens": u.get("completion_tokens"), "seconds": round(time.time() - t0, 1)}


def compare_identity(pro: list, flash: list, expected: list) -> dict:
    """Is the 'advanced' tier distinguishable from the cheap one? A heuristic, not proof."""
    def stats(rs):
        ok = [r for r in rs if "error" not in r]
        toks = [r["out_tokens"] for r in ok if r.get("out_tokens")]
        return {"answered": len(ok),
                "correct": sum(r.get("answer") == x for r, x in zip(rs, expected) if "error" not in r),
                "mean_out_tokens": round(sum(toks) / len(toks)) if toks else None,
                "models": sorted({str(r.get("model")) for r in ok}),
                "errors": [r["error"] for r in rs if "error" in r][:3]}
    p, f = stats(pro), stats(flash)
    warnings = []
    if p["answered"] == 0:
        warnings.append("the advanced tier answered nothing: check its model id against the listed ids")
    if p["models"] and p["models"] == f["models"]:
        warnings.append(f"both tiers report the same model id {p['models']}: the advanced id may be an alias of the cheap one")
    same_answers = all(a.get("answer") == b.get("answer") for a, b in zip(pro, flash))
    pt, ft = p["mean_out_tokens"], f["mean_out_tokens"]
    if pt and ft and same_answers and abs(pt - ft) <= 0.15 * max(pt, ft):
        warnings.append("identical answers and a near-identical reasoning-length profile: the advanced tier is "
                        "indistinguishable on this set; treat its role as unproven until the benchmark")
    if p["correct"] < f["correct"]:
        warnings.append(f"the advanced tier scored below the cheap one ({p['correct']} vs {f['correct']} of {len(expected)})")
    return {"pro": p, "flash": f, "warnings": warnings}


def identity(e: dict, tiers: dict, pair: list) -> dict:
    a, b = (tiers[n] for n in pair)
    pro = [ask_direct(e, a, q) for q, _ in QUIZ]
    fl = [ask_direct(e, b, q) for q, _ in QUIZ]
    return {"pair": pair, "ids": [model_id(a), model_id(b)], **compare_identity(pro, fl, [x for _, x in QUIZ]),
            "raw": {"pro": pro, "flash": fl}}


def write_report(e: dict, report: dict) -> None:
    out = Path(e.get("PROBE_OUT") or HERE.parent / ".loop" / "probe.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"details: {out}")


def main() -> int:
    e = env_view()
    tiers = load_tiers(env=e)
    args = sys.argv[1:]
    if "--identity" in args:
        pair = probe_settings().get("identity") or ["ds-pro", "ds-flash"]
        rep = identity(e, tiers, pair)
        for label, name, mid in (("pro", pair[0], rep["ids"][0]), ("flash", pair[1], rep["ids"][1])):
            s = rep[label]
            print(f"{name:12} {mid:28} correct {s['correct']}/{len(QUIZ)} answered {s['answered']} "
                  f"mean_out_tokens {s['mean_out_tokens']} reported_model {','.join(s['models']) or '-'}")
        for w in rep["warnings"]:
            print(f"WARN {w}")
        if not rep["warnings"]:
            print(f"PASS {pair[0]} and {pair[1]} are distinguishable on this set (identity only; quality needs the benchmark)")
        write_report(e, {"identity": rep})
        return 1 if rep["pro"]["answered"] == 0 else 0
    unknown = [a for a in args if a not in tiers]
    if unknown:
        print(f"error: not tiers in tiers.toml: {unknown}", file=sys.stderr)
        return 2
    only = args or list(tiers)
    base = f"http://127.0.0.1:{e.get('GATEWAY_PORT', '4000')}"
    key = e.get("GATEWAY_KEY", "sk-devteam-local")
    ids = listed_ids(e, {n: tiers[n] for n in only})
    report = {"tiers": {}}
    for name in only:
        t = tiers[name]
        listed = (model_id(t) in ids[name]) if isinstance(ids[name], list) else None
        res = tool_loop(base, key, name, "high" if t["think_at"] != "never" else "medium")
        res.update({"model": t["model"], "listed_upstream": listed})
        report["tiers"][name] = res
        extra = (f"{res.get('seconds')}s thinking={res.get('thinking_blocks')}" if res["ok"]
                 else f"{res.get('step')}: {str(res.get('error'))[:140]}")
        print(f"{'PASS' if res['ok'] else 'FAIL'} {name:13} {t['model']:55} listed={listed} {extra}")
    if not args:  # a role route resolves through the active profile
        res = tool_loop(base, key, "role-runner", "low")
        report["tiers"]["role-runner"] = res
        print(f"{'PASS' if res['ok'] else 'FAIL'} {'role-runner':13} via profile {e.get('PROFILE') or 'baseline'}"
              + ("" if res["ok"] else f" {res.get('step')}: {str(res.get('error'))[:140]}"))
    unlisted = [n for n in only if report["tiers"][n].get("listed_upstream") is False]
    if unlisted:
        print(f"note: model id not in its endpoint's /models list for {', '.join(unlisted)} (aliases can still work)")
    write_report(e, report)
    return 0 if all(r["ok"] for r in report["tiers"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
