"""Load and validate gateway/tiers.toml, the single source of truth for which models exist.

Used by hooks.py (per-tier request policy), render.py (the LiteLLM config), probe.py (live checks)
and harness/tokens.py (prices). Paths can be overridden for tests and experiments:
TIERS_FILE=<path to a tiers .toml>, PROFILES_DIR=<dir of profile .toml files>.
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
EFFORT = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
THINKING = ("deepseek", "none")
DEFAULTS = {"api_key_env": "", "thinking": "none", "think_at": "never", "vision": False, "max_output": 16384,
            "rpm": 0, "timeout": 600, "fallbacks": [], "price": [0, 0, 0], "compare_to": "sonnet", "data": "",
            "extra_body": {}}
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class TierError(ValueError):
    pass


def dotenv(path: Path | None = None) -> dict:
    """KEY=VALUE pairs from gateway/.env (comments and blanks ignored)."""
    f = path or HERE / ".env"
    out = {}
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def env_view() -> dict:
    """The process environment, with gateway/.env filling anything it does not set."""
    return {**dotenv(), **os.environ}


def expand(s: str, env: dict) -> str:
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), s)


def tiers_file() -> Path:
    return Path(os.environ.get("TIERS_FILE") or HERE / "tiers.toml")


def profiles_dir() -> Path:
    return Path(os.environ.get("PROFILES_DIR") or HERE / "profiles")


def load_tiers(path: Path | None = None, env: dict | None = None) -> dict:
    """name -> tier dict with defaults applied and ${VAR} expanded. Raises TierError on any problem."""
    f = path or tiers_file()
    if not f.exists():
        raise TierError(f"no tiers file at {f}")
    raw = tomllib.loads(f.read_text()).get("tiers") or {}
    if not raw:
        raise TierError(f"{f} defines no [tiers.<name>] blocks")
    e = env if env is not None else env_view()
    out = {}
    for name, t in raw.items():
        t = {**DEFAULTS, **t}
        for k in ("model", "api_base", "api_key_env"):
            t[k] = expand(str(t.get(k) or ""), e)
        problems = []
        if "/" not in t["model"]:
            problems.append('model must be "<litellm provider>/<model id>", e.g. "openai/my-model"')
        if t["thinking"] not in THINKING:
            problems.append(f"thinking must be one of {THINKING}")
        if t["think_at"] != "never" and t["think_at"] not in EFFORT:
            problems.append(f"think_at must be never or one of {list(EFFORT)}")
        if t["think_at"] != "never" and t["thinking"] == "none":
            problems.append('think_at needs a thinking switch; use thinking = "deepseek" or think_at = "never"')
        if not (isinstance(t["price"], list) and len(t["price"]) == 3):
            problems.append("price must be [input, output, cache_read] per million tokens")
        if problems:
            raise TierError(f"tier {name!r} in {f}: " + "; ".join(problems))
        out[name] = t
    for name, t in out.items():
        bad = [x for x in t["fallbacks"] if x not in out or x == name]
        if bad:
            raise TierError(f"tier {name!r} falls back to unknown or self tiers {bad}")
    return out


def probe_settings(path: Path | None = None) -> dict:
    f = path or tiers_file()
    return tomllib.loads(f.read_text()).get("probe") or {}


def model_id(t: dict) -> str:
    """The id the upstream API expects: the LiteLLM model string without its provider prefix."""
    return t["model"].split("/", 1)[1]
