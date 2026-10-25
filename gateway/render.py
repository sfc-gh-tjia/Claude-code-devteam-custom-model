#!/usr/bin/env python3
"""Render the LiteLLM proxy config from gateway/tiers.toml (start.sh runs this; don't edit the output).

    python3 gateway/render.py                    # -> gateway/.litellm.rendered.yaml
    python3 gateway/render.py --out <file>       # the file must sit in gateway/: LiteLLM resolves
                                                 # `callbacks: hooks.normalize` next to the config

The output is JSON, which YAML loaders read unchanged. Also validates the active profile against
the tiers, so a broken profile or tier stops the gateway at start, not mid-run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]
from tiers import TierError, load_tiers  # noqa: E402


def render(tiers: dict) -> dict:
    model_list = []
    for name, t in tiers.items():
        p = {"model": t["model"], "timeout": t["timeout"], "stream_timeout": t["timeout"],
             # a keyless local server still needs a non-empty key for OpenAI-compatible clients
             "api_key": f"os.environ/{t['api_key_env']}" if t["api_key_env"] else "sk-none"}
        if t["api_base"]:
            p["api_base"] = t["api_base"]
        if t["rpm"]:
            p["rpm"] = t["rpm"]
        model_list.append({"model_name": name, "litellm_params": p})
    return {
        "model_list": model_list,
        "litellm_settings": {"drop_params": True, "callbacks": "hooks.normalize", "num_retries": 2,
                             "request_timeout": 600},
        "router_settings": {
            "fallbacks": [{n: t["fallbacks"]} for n, t in tiers.items() if t["fallbacks"]],
            "retry_policy": {"RateLimitErrorRetries": 2, "TimeoutErrorRetries": 1, "InternalServerErrorRetries": 2},
            "allowed_fails": 3, "cooldown_time": 60,
        },
        "general_settings": {"master_key": "os.environ/GATEWAY_KEY"},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(HERE / ".litellm.rendered.yaml"))
    a = ap.parse_args()
    try:
        tiers = load_tiers()
        from hooks import active_profile  # validates the profile against these tiers
        name, roles = active_profile()
    except (TierError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = Path(a.out)
    out.write_text(json.dumps(render(tiers), indent=2) + "\n")
    print(f"rendered {len(tiers)} tiers, profile {name} ({len(roles)} roles) -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
