#!/usr/bin/env python3
"""Doctor check: is the service this project points at really a working DevTeam gateway?

    python3 harness/gateway_check.py            # identity, auth, profile, one tiny free request
    python3 harness/gateway_check.py --no-call  # skip the request (no model is called at all)

Reads the connection from .claude/settings.json (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN and the
x-devteam-profile header in ANTHROPIC_CUSTOM_HEADERS), so it checks exactly what this project's
agents will use. Any HTTP server on port 4000 answers /health; only the DevTeam hook rejects an
unknown role with its own message, so that is the identity check. Stdlib only.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IDENTITY_ROLE = "role-devteam-identity-check"
IDENTITY_MARK = "is not defined in the active profile"


def settings(root: Path) -> dict:
    env = (json.loads((root / ".claude" / "settings.json").read_text()).get("env") or {})
    headers = {}
    for line in str(env.get("ANTHROPIC_CUSTOM_HEADERS") or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return {"base": (env.get("ANTHROPIC_BASE_URL") or "http://127.0.0.1:4000").rstrip("/"),
            "token": env.get("ANTHROPIC_AUTH_TOKEN") or "", "headers": headers}


def call(s: dict, path: str, body: dict | None = None, timeout: int = 60):
    h = {"content-type": "application/json", "authorization": f"Bearer {s['token']}", "x-api-key": s["token"],
         "anthropic-version": "2023-06-01", **s["headers"]}
    r = urllib.request.Request(s["base"] + path, data=json.dumps(body).encode() if body is not None else None,
                               method="POST" if body is not None else "GET", headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def check(root: Path = ROOT, no_call: bool = False) -> list:
    """[(ok, message)] in order; stops at the first failure."""
    s = settings(root)
    prof = next((v for k, v in s["headers"].items() if k.lower() == "x-devteam-profile"), "(gateway default)")
    out = []
    code, body = call(s, "/health/liveliness", timeout=5)
    if code != 200:
        return [(False, f"nothing healthy at {s['base']} ({code or body[:80]}): start it with `make gateway`")]
    out.append((True, f"reachable: {s['base']}"))
    msg = {"model": IDENTITY_ROLE, "max_tokens": 1, "messages": [{"role": "user", "content": "x"}]}
    code, body = call(s, "/v1/messages", msg, timeout=15)
    # LiteLLM answers an unknown key with 401, or (master key, no database) 400 "no_db_connection"
    if code in (401, 403) or "no_db_connection" in body or "Authentication Error" in body:
        return out + [(False, f"the gateway rejected this project's token (HTTP {code}): ANTHROPIC_AUTH_TOKEN in "
                              ".claude/settings.json must equal GATEWAY_KEY in the gateway's .env")]
    if "not allowed" in body:
        return out + [(False, f"profile {prof!r} is not available on this gateway: {body[:200]}")]
    if IDENTITY_MARK not in body:
        return out + [(False, f"the service at {s['base']} is not a DevTeam gateway (or an old one): "
                              f"HTTP {code} {body[:120]}")]
    out.append((True, f"DevTeam gateway; token accepted; profile {prof} available"))
    if no_call:
        return out
    msg = {"model": "role-runner", "max_tokens": 16, "messages": [{"role": "user", "content": "Reply with OK."}]}
    code, body = call(s, "/v1/messages", msg, timeout=120)
    try:
        ok = code == 200 and json.loads(body).get("type") == "message"
    except ValueError:
        ok = False
    out.append((ok, "role-runner answered (one tiny request on the runner's tier)" if ok
                else f"role-runner failed: HTTP {code} {body[:200]} -> run `make probe` on the gateway"))
    return out


def main() -> int:
    res = check(no_call="--no-call" in sys.argv[1:])
    for ok, m in res:
        print(("ok   " if ok else "FAIL ") + m)
    return 0 if res and all(ok for ok, _ in res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
