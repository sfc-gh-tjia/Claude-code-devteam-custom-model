"""Gateway pre-call hook: resolve role routes, and make Claude Code's requests safe and cheap per tier.

Agents name a role (model: role-reviewer). The active profile (gateway/profiles/<PROFILE>.toml)
maps each role to a tier, and gateway/tiers.toml defines each tier: its model and endpoint, and the
policy below. So a benchmark configuration is a profile switch and a new model is a tier block,
never an agent edit. An unknown role fails the request instead of guessing.

Claude Code sends every model the same Anthropic-shaped request: adaptive thinking, an
`output_config.effort`, Claude-sized `max_tokens`, images from screenshots, and the thinking blocks
of earlier turns. Most other models accept only some of that. `normalize_request` rewrites one
request for its tier; it is a pure function so the tests can pin it without LiteLLM.

Token policy (the point of this file):
- Thinking is spent only where it decides: a tier thinks at or above its `think_at` effort
  (baseline: ds-pro at medium, ds-flash at high, free tiers never).
- Old thinking blocks are dropped from history wherever thinking is off: they are re-sent (and
  billed) on every turn otherwise, and other model families reject them anyway.
- Nothing is ever added in front of the prompt, so prefix caches (DeepSeek's is automatic, a hit
  costs about 1/30 to 1/50 of a miss) keep hitting.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
import tomllib
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent)]
from tiers import EFFORT, load_tiers, profiles_dir  # noqa: E402

ANTHROPIC_ONLY = ("output_config", "context_management", "container", "mcp_servers", "betas")
# Only these request keys are read or rewritten; LiteLLM's own keys (metadata, litellm_*) are left alone.
OWNED = ("model", "messages", "system", "thinking", "max_tokens", "extra_body") + ANTHROPIC_ONLY
IMAGE_NOTE = "[image omitted: this model has no vision; the ui-reviewer's tier can look at it]"


def build_routes(tiers: dict) -> dict:
    return {n: {"think_at": None if t["think_at"] == "never" else EFFORT[t["think_at"]], "vision": t["vision"],
                "max_out": t["max_output"], "deepseek": t["thinking"] == "deepseek", "extra_body": t["extra_body"]}
            for n, t in tiers.items()}


_TIERS = load_tiers()
ROUTES = build_routes(_TIERS)
TIER_OF_MODEL = {t["model"]: n for n, t in _TIERS.items()}   # first tier wins if two share a model
DEFAULT_ROUTE = "ds-flash" if "ds-flash" in ROUTES else next(iter(ROUTES))  # stray non-role ids (no profile)


class ProfileError(ValueError):
    pass


def load_profile(name: str, _seen: tuple = ()) -> dict:
    """Role -> tier map for a profile, with `extends` applied. Raises ProfileError on any problem."""
    if name in _seen:
        raise ProfileError(f"profile cycle: {' -> '.join(_seen + (name,))}")
    d = profiles_dir()
    f = d / f"{name}.toml"
    if not f.exists():
        raise ProfileError(f"no profile {name!r} in {d} (have: {', '.join(sorted(p.stem for p in d.glob('*.toml')))})")
    p = tomllib.loads(f.read_text())
    roles = dict(load_profile(p["extends"], _seen + (name,))) if p.get("extends") else {}
    roles.update(p.get("roles") or {})
    bad = {r: t for r, t in roles.items() if t not in ROUTES}
    if bad:
        raise ProfileError(f"profile {name!r} maps roles to tiers not in tiers.toml: {bad}")
    return roles


def active_profile() -> tuple[str, dict]:
    name = os.environ.get("PROFILE") or "baseline"
    return name, load_profile(name)


# Per-session profiles: a client names its profile in this request header (Claude Code sends it via
# ANTHROPIC_CUSTOM_HEADERS), so one gateway serves sessions with different profiles at once. Without
# the header, the gateway's PROFILE applies. The header only selects among profiles this gateway
# allows (ALLOWED_PROFILES, comma-separated; default: every file in profiles/). It is a routing choice
# on a localhost gateway, not an authorization mechanism: a shared deployment should bind allowed
# profiles to per-user API keys instead.
PROFILE_HEADER = "x-devteam-profile"


def allowed_profiles() -> list:
    names = [n.strip() for n in (os.environ.get("ALLOWED_PROFILES") or "").split(",") if n.strip()]
    return names or sorted(p.stem for p in profiles_dir().glob("*.toml"))


def profile_for(headers: dict | None, default: str, allowed) -> str:
    """The profile one request asked for (case-insensitive header), or the default. Fails closed."""
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    name = (h.get(PROFILE_HEADER) or "").strip() or default
    if name not in allowed:
        raise ProfileError(f"profile {name!r} is not allowed on this gateway (allowed: {', '.join(allowed)})")
    return name


def route_log_path() -> Path:
    return Path(os.environ.get("GATEWAY_ROUTE_LOG") or Path(__file__).resolve().parent.parent / ".loop" / "gateway-routes.jsonl")


def log_route(rec: dict) -> None:
    """Append one JSON line; logging never breaks a request."""
    try:
        p = route_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec}
        with p.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def resolve(model: str | None, roles: dict) -> str:
    """role-<name> -> its tier via the profile (unknown role: ProfileError). A tier name passes through;
    any other id (e.g. a stray claude-* name) goes to the profile's `default` role."""
    m = (model or "").strip()
    if m.startswith("role-"):
        if m[5:] not in roles:
            raise ProfileError(f"role route {m!r} is not defined in the active profile")
        return roles[m[5:]]
    return m if m in ROUTES else roles.get("default", DEFAULT_ROUTE)


def route_of(model: str | None) -> str:
    m = (model or "").strip()
    return m if m in ROUTES else DEFAULT_ROUTE


def _strip_blocks(content, drop_thinking: bool, drop_images: bool):
    if not isinstance(content, list):
        return content
    out = []
    for b in content:
        if not isinstance(b, dict):
            out.append(b)
            continue
        t = b.get("type")
        if drop_thinking and t in ("thinking", "redacted_thinking"):
            continue
        if drop_images and t in ("image", "document") and (b.get("source") or {}).get("type") in ("base64", "url"):
            out.append({"type": "text", "text": IMAGE_NOTE})
            continue
        if t == "tool_result" and isinstance(b.get("content"), list):
            b = {**b, "content": _strip_blocks(b["content"], drop_thinking, drop_images) or [{"type": "text", "text": ""}]}
        out.append(b)
    return out


def normalize_request(data: dict, roles: dict | None = None) -> dict:
    """Return a copy of an Anthropic Messages request, resolved to its tier and rewritten for it."""
    d = copy.deepcopy(data)
    if roles is not None:
        d["model"] = resolve(d.get("model"), roles)
    d["model"] = route_of(d.get("model"))
    r = ROUTES[d["model"]]
    effort = ((d.get("output_config") or {}).get("effort") or "high").lower()
    for k in ANTHROPIC_ONLY:
        d.pop(k, None)

    wants = d.pop("thinking", None)
    think = (r["think_at"] is not None and EFFORT.get(effort, 2) >= r["think_at"]
             and (wants or {}).get("type") != "disabled")
    if r.get("max_out") and int(d.get("max_tokens") or 0) > r["max_out"]:
        d["max_tokens"] = r["max_out"]
    extra = {**(d.get("extra_body") or {}), **(r.get("extra_body") or {})}
    if r.get("deepseek"):
        # DeepSeek's switch is {"type": "enabled"|"disabled"} with no budget. LiteLLM 1.80 drops a
        # `thinking`/`reasoning_effort` param for DeepSeek, so it travels in extra_body, which
        # LiteLLM forwards verbatim. It is always explicit so exec roles never think by default.
        extra["thinking"] = {"type": "enabled" if think else "disabled"}
    # thinking = "none" tiers get no thinking param at all: each model has its own switch, and an
    # unverified one can 400. A tier that needs one sets it in its `extra_body`.
    if extra:
        d["extra_body"] = extra

    drop_images = not r["vision"]
    if isinstance(d.get("messages"), list):
        d["messages"] = [{**m, "content": _strip_blocks(m.get("content"), not think, drop_images)}
                         if isinstance(m, dict) else m for m in d["messages"]]
    if isinstance(d.get("system"), list):   # Anthropic cache_control markers mean nothing upstream
        d["system"] = [{k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b
                       for b in d["system"]]
    return d


try:  # the LiteLLM wrapper; absent when the pure function is imported by the tests
    from litellm.integrations.custom_logger import CustomLogger

    class Normalize(CustomLogger):
        def __init__(self):
            super().__init__()
            self.profile, _ = active_profile()
            self.allowed = allowed_profiles()
            if self.profile not in self.allowed:
                raise ProfileError(f"PROFILE {self.profile!r} is not in ALLOWED_PROFILES {self.allowed}")
            # every allowed profile is loaded and validated now: a broken one stops the gateway at start,
            # and requests only read this map (no shared state changes per request)
            self.profiles = {n: load_profile(n) for n in self.allowed}
            self.inflight: dict = {}   # litellm_call_id -> route, until its success/failure callback

        async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
            if isinstance(data, dict) and "messages" in data:
                headers = (data.get("proxy_server_request") or {}).get("headers") or {}
                prof = profile_for(headers, self.profile, self.allowed)
                requested = data.get("model")
                new = normalize_request({k: data[k] for k in OWNED if k in data}, self.profiles[prof])
                for k in OWNED:
                    if k in new:
                        data[k] = new[k]
                    else:
                        data.pop(k, None)
                route = {"profile": prof, "requested": requested, "tier": data.get("model")}
                cid = data.get("litellm_call_id")
                if cid:   # joined back by the success/failure callbacks (same litellm_call_id)
                    if len(self.inflight) > 10_000:
                        self.inflight.clear()
                    self.inflight[cid] = route
                log_route({"event": "route", "id": cid, **route})
            return data

        def _served(self, kwargs, response_obj, start_time, end_time, ok: bool) -> dict:
            slo = kwargs.get("standard_logging_object") or {}
            cid = kwargs.get("litellm_call_id")
            # a failed attempt may still be rescued by a fallback under the same call id: keep the route
            route = (self.inflight.pop(cid, None) if ok else self.inflight.get(cid)) or {}
            model = slo.get("model") or kwargs.get("model")
            # after a fallback LiteLLM still reports the original model_group; the model that answered
            # identifies the tier that really served the request
            served_tier = TIER_OF_MODEL.get(str(model)) or slo.get("model_group")
            rec = {"event": "served" if ok else "failed", "id": cid, **route,
                   "served_tier": served_tier, "served_model": model,
                   "input_tokens": slo.get("prompt_tokens"), "output_tokens": slo.get("completion_tokens"),
                   "seconds": round((end_time - start_time).total_seconds(), 2) if start_time and end_time else None}
            rec["fallback"] = bool(route.get("tier") and served_tier and served_tier != route["tier"])
            if not ok:
                rec["error"] = str(kwargs.get("exception") or slo.get("error_str") or "")[:300]
            return rec

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
            log_route(self._served(kwargs, response_obj, start_time, end_time, True))

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
            log_route(self._served(kwargs, response_obj, start_time, end_time, False))

    normalize = Normalize()
except ImportError:  # pragma: no cover
    normalize = None
