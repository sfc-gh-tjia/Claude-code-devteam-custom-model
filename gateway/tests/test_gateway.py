"""Gateway tests, no API keys needed.

1. normalize_request: the per-route policy (thinking, effort, images, max_tokens, history).
2. The real LiteLLM proxy, configured by render.py from tiers.toml, with hooks.py, in front of a mock OpenAI-
   compatible upstream that stands in for both DeepSeek and NVIDIA. Checks routing, translation,
   tool calls, streaming and fallback on a free-tier 429.
3. Real Claude Code through that proxy: the lead (an alias) and a subagent (a route name in its
   frontmatter) reach the right upstream models, and a tool call round-trips.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

GW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GW))
from hooks import IMAGE_NOTE, PROFILE_HEADER, ROUTES, ProfileError, load_profile, normalize_request, profile_for, resolve  # noqa: E402
from render import render  # noqa: E402
from tiers import TierError, load_tiers, profiles_dir  # noqa: E402
from probe import QUIZ, compare_identity  # noqa: E402

KEY = "sk-test"
IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


def req(model, effort="high", **kw):
    d = {"model": model, "max_tokens": 64000, "thinking": {"type": "adaptive"},
         "output_config": {"effort": effort}, "messages": [{"role": "user", "content": "hi"}]}
    d.update(kw)
    return d


# ------------------------------------------------------------------ 1. policy
def test_pro_thinks_and_drops_anthropic_only_fields():
    d = normalize_request(req("ds-pro", context_management={"x": 1}))
    assert d["extra_body"]["thinking"] == {"type": "enabled"} and "thinking" not in d
    assert "output_config" not in d and "context_management" not in d


def test_flash_thinks_only_at_high_effort():
    mode = lambda m, e: normalize_request(req(m, e))["extra_body"]["thinking"]["type"]  # noqa: E731
    assert mode("ds-flash", "high") == "enabled" and mode("ds-flash", "medium") == "disabled"
    assert mode("ds-pro", "medium") == "enabled" and mode("ds-pro", "low") == "disabled"


def test_free_routes_never_think_and_clamp_output():
    for m in ("nv-fast", "nv-smart", "nv-vision"):
        d = normalize_request(req(m, "max"))
        assert "thinking" not in d and "extra_body" not in d and d["max_tokens"] <= 16384
    assert normalize_request(req("nv-fast"))["max_tokens"] == 8192


def test_history_thinking_dropped_when_not_thinking_kept_when_thinking():
    hist = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "...", "signature": "s"},
                                              {"type": "text", "text": "a"}]},
            {"role": "user", "content": "q2"}]
    nv = normalize_request(req("nv-smart", messages=hist))
    assert [b["type"] for b in nv["messages"][1]["content"]] == ["text"]
    ds = normalize_request(req("ds-pro", messages=hist))
    assert [b["type"] for b in ds["messages"][1]["content"]] == ["thinking", "text"]


def test_images_replaced_on_text_routes_kept_on_vision_routes():
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [IMG]}, IMG]}]
    pro = normalize_request(req("ds-pro", messages=msgs))["messages"][0]["content"]
    assert pro[0]["content"][0]["text"] == IMAGE_NOTE and pro[1]["text"] == IMAGE_NOTE
    vis = normalize_request(req("nv-vision", messages=msgs))["messages"][0]["content"]
    assert vis[1]["type"] == "image"


def test_unknown_model_is_exec_tier_and_input_not_mutated():
    src = req("claude-sonnet-5-5", "high")
    d = normalize_request(src)
    assert d["extra_body"]["thinking"]["type"] == "enabled" and src["output_config"] == {"effort": "high"}


# ------------------------------------------------------------------ 1b. profiles and role routes
ROOT = GW.parent


def role_routes_in_use() -> set:
    """Every role route the harness can send: agent frontmatter, settings env, workflow constants."""
    import re
    used = {re.search(r"(?m)^model: (\S+)", f.read_text()).group(1) for f in (ROOT / ".claude" / "agents").glob("*.md")}
    env = json.loads((ROOT / ".claude" / "settings.json").read_text())["env"]
    used |= {v for k, v in env.items() if k.endswith("_MODEL") or k == "CLAUDE_CODE_SUBAGENT_MODEL"}
    used |= set(re.findall(r"'(role-[a-z-]+)'", (ROOT / ".claude" / "workflows" / "build-phase.js").read_text()))
    return used


def test_every_role_route_in_use_is_defined_in_every_profile():
    used = role_routes_in_use()
    assert used and all(u.startswith("role-") for u in used), used
    for f in profiles_dir().glob("*.toml"):
        roles = load_profile(f.stem)
        missing = sorted(u for u in used if u[5:] not in roles)
        assert not missing, (f.stem, missing)


def test_profiles_extend_and_override():
    base, glm, ff = load_profile("baseline"), load_profile("glm-free"), load_profile("flash-first")
    assert base["reviewer"] == "ds-pro" and ff["reviewer"] == "ds-flash" and ff["implementer"] == base["implementer"]
    assert glm["triage"] == "nv-glm-flash" and glm["cross-reviewer"] == "nv-smart-alt" and glm["reviewer"] == "ds-pro"
    assert ff["escalate"] == "ds-pro"   # flash-first still escalates to the advanced tier


def test_role_resolution_fails_closed():
    roles = load_profile("baseline")
    assert resolve("role-reviewer", roles) == "ds-pro" and resolve("nv-fast", roles) == "nv-fast"
    with pytest.raises(ProfileError):
        resolve("role-no-such-role", roles)
    with pytest.raises(ProfileError):
        load_profile("no-such-profile")
    d = normalize_request(req("role-reviewer", "high"), roles)
    assert d["model"] == "ds-pro" and d["extra_body"]["thinking"] == {"type": "enabled"}


def test_identity_comparison_flags_an_alias():
    exp = [x for _, x in QUIZ]
    same = [{"model": "deepseek-flash", "answer": x, "out_tokens": 900} for x in exp]
    rep = compare_identity(same, [dict(r) for r in same], exp)
    assert any("same model id" in w for w in rep["warnings"]) and any("indistinguishable" in w for w in rep["warnings"])
    pro = [{"model": "deepseek-v4-pro", "answer": x, "out_tokens": 2400} for x in exp]
    flash = [{"model": "deepseek-flash", "answer": x if i % 2 else -1, "out_tokens": 900} for i, x in enumerate(exp)]
    assert compare_identity(pro, flash, exp)["warnings"] == []
    dead = compare_identity([{"error": "HTTP 404"}] * len(exp), flash, exp)
    assert any("answered nothing" in w for w in dead["warnings"])


# ------------------------------------------------------------------ 1c. tiers.toml: the plug-in point
TIER_OK = """
[tiers.a]
model = "openai/m1"
api_base = "${X_BASE:-http://default/v1}"
api_key_env = "X_KEY"
fallbacks = ["b"]
rpm = 10
[tiers.b]
model = "deepseek/m2"
thinking = "deepseek"
think_at = "high"
"""


def test_tiers_load_with_defaults_and_env_expansion(tmp_path):
    f = tmp_path / "t.toml"
    f.write_text(TIER_OK)
    t = load_tiers(f, env={})
    assert t["a"]["api_base"] == "http://default/v1" and t["a"]["vision"] is False and t["a"]["max_output"] == 16384
    assert load_tiers(f, env={"X_BASE": "http://proxy/v1"})["a"]["api_base"] == "http://proxy/v1"
    cfg = render(t)
    a = next(m for m in cfg["model_list"] if m["model_name"] == "a")["litellm_params"]
    assert a == {"model": "openai/m1", "api_base": "http://default/v1", "api_key": "os.environ/X_KEY", "rpm": 10,
                 "timeout": 600, "stream_timeout": 600}
    b = next(m for m in cfg["model_list"] if m["model_name"] == "b")["litellm_params"]
    assert b["api_key"] == "sk-none" and "api_base" not in b          # keyless, provider default endpoint
    assert cfg["router_settings"]["fallbacks"] == [{"a": ["b"]}]
    assert cfg["litellm_settings"]["callbacks"] == "hooks.normalize"


@pytest.mark.parametrize("bad, msg", [
    ('[tiers.x]\nmodel = "no-provider"', "provider"),
    ('[tiers.x]\nmodel = "openai/m"\nthink_at = "high"', "thinking switch"),
    ('[tiers.x]\nmodel = "openai/m"\nthinking = "magic"', "thinking must be"),
    ('[tiers.x]\nmodel = "openai/m"\nfallbacks = ["nope"]', "unknown or self"),
    ('[tiers.x]\nmodel = "openai/m"\nprice = [1, 2]', "price"),
])
def test_bad_tiers_are_rejected_with_a_clear_message(tmp_path, bad, msg):
    f = tmp_path / "t.toml"
    f.write_text(bad)
    with pytest.raises(TierError, match=msg):
        load_tiers(f, env={})


def test_shipped_tiers_and_profiles_are_consistent():
    tiers = load_tiers(env={})
    assert set(tiers) == set(ROUTES)
    for f in profiles_dir().glob("*.toml"):
        assert set(load_profile(f.stem).values()) <= set(tiers), f.stem


def test_profile_header_is_case_insensitive_defaults_and_fails_closed():
    allowed = ["baseline", "interactive"]
    assert profile_for({}, "baseline", allowed) == "baseline"
    assert profile_for({"X-DevTeam-Profile": "interactive"}, "baseline", allowed) == "interactive"
    assert profile_for({PROFILE_HEADER: "  "}, "baseline", allowed) == "baseline"
    with pytest.raises(ProfileError, match="not allowed"):
        profile_for({PROFILE_HEADER: "flash-first"}, "baseline", allowed)
    assert load_profile("interactive")["background"] == "nv-smart" and load_profile("baseline")["background"] == "nv-fast"


# ------------------------------------------------------------------ 2. LiteLLM proxy + mock upstream
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Upstream:
    """OpenAI-compatible mock. Records every request; behaviour is keyed off the request."""

    def __init__(self):
        self.calls: list[dict] = []
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._json(200, {"object": "list", "data": []})

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                body["_path"], body["_auth"] = self.path, self.headers.get("authorization", "")
                body["_headers"] = {k.lower(): v for k, v in self.headers.items()}
                up.calls.append(body)
                msgs = body.get("messages", [])
                text = json.dumps(msgs)
                if "FORCE429" in text and "kimi" in body.get("model", ""):
                    return self._json(429, {"error": {"message": "rate limited", "type": "rate_limit"}})
                tools = [t["function"]["name"] for t in body.get("tools", []) if t.get("type") == "function"]
                has_result = any(m.get("role") == "tool" for m in msgs)
                call = None
                if "Agent" in tools and not has_result:
                    call = {"name": "Agent", "arguments": json.dumps(
                        {"subagent_type": "probe", "description": "probe", "prompt": "say hi"})}
                elif ("CALLTOOL" in text or "Use the tool" in text) and tools and not has_result:
                    call = {"name": tools[0], "arguments": json.dumps({"city": "Paris"})}
                msg = {"role": "assistant", "content": None if call else "ok"}
                if call:
                    msg["tool_calls"] = [{"id": "call_1", "type": "function", "function": call}]
                usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                         "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 4}
                if not body.get("stream"):
                    return self._json(200, {"id": "c1", "object": "chat.completion", "model": body.get("model"),
                                            "choices": [{"index": 0, "message": msg,
                                                         "finish_reason": "tool_calls" if call else "stop"}],
                                            "usage": usage})
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()

                def chunk(delta, finish=None, use=None):
                    c = {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"),
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    if use:
                        c["usage"] = use
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                chunk({"role": "assistant", "content": ""})
                if call:
                    chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                           "function": {"name": call["name"], "arguments": call["arguments"]}}]})
                    chunk({}, "tool_calls", usage)
                else:
                    chunk({"content": "ok"})
                    chunk({}, "stop", usage)
                self.wfile.write(b"data: [DONE]\n\n")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def models(self):
        return [c.get("model") for c in self.calls]


def start_gateway(up, extra=None):
    """Render the config from tiers.toml (as start.sh does) and run the real LiteLLM proxy on it."""
    port = free_port()
    env = {**os.environ, "DEEPSEEK_API_KEY": "ds-test", "NVIDIA_API_KEY": "nv-test",
           "DEEPSEEK_API_BASE": up.url, "NVIDIA_API_BASE": up.url + "/v1",
           "PROFILE": "baseline", "GATEWAY_KEY": KEY, "LITELLM_LOG": "ERROR",
           "GATEWAY_ROUTE_LOG": tempfile.mktemp(suffix=".jsonl"), **(extra or {})}
    cfg = GW / f".litellm.test-{port}.yaml"   # must sit in gateway/: LiteLLM imports hooks.py next to it
    r = subprocess.run([sys.executable, str(GW / "render.py"), "--out", str(cfg)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    p = subprocess.Popen(["litellm", "--config", str(cfg), "--host", "127.0.0.1", "--port", str(port)],
                         cwd=GW, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/health/liveliness", timeout=1)
            break
        except OSError:
            if p.poll() is not None:
                break
            time.sleep(0.5)
    if p.poll() is not None or not _up(base):
        p.kill()
        cfg.unlink(missing_ok=True)
        pytest.fail("gateway did not start:\n" + Path(log.name).read_text()[-3000:])
    ROUTE_LOGS[base] = Path(env["GATEWAY_ROUTE_LOG"])
    return base, p, log.name, cfg


ROUTE_LOGS: dict = {}


def routes(base) -> list:
    f = ROUTE_LOGS[base]
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def _up(base):
    try:
        urllib.request.urlopen(base + "/health/liveliness", timeout=1)
        return True
    except OSError:
        return False


def stop_gateway(p, cfg):
    os.killpg(p.pid, 15)
    p.wait(10)
    cfg.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def gateway():
    if not shutil.which("litellm"):
        pytest.skip("litellm not installed (pip install -r gateway/requirements.txt)")
    up = Upstream()
    base, p, log, cfg = start_gateway(up)
    yield base, up, log
    stop_gateway(p, cfg)


def post(base, body, stream=False, headers=None):
    body = {**body, "stream": stream}
    r = urllib.request.Request(base + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                               headers={"content-type": "application/json", "x-api-key": KEY,
                                        "authorization": f"Bearer {KEY}", "anthropic-version": "2023-06-01",
                                        **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise AssertionError(f"{e.code}: {e.read().decode()[:2000]}")
    return raw if stream else json.loads(raw)


def last(up, n=1):
    return up.calls[-n:]


def test_routes_reach_the_right_upstream(gateway):
    base, up, _ = gateway
    want = {"ds-pro": "deepseek-v4-pro", "ds-flash": "deepseek-flash",
            "nv-fast": "nvidia/nemotron-3.5-lightning-30b-a3b", "nv-smart": "moonshotai/kimi-k2.6",
            "nv-vision": "nvidia/nemotron-3.5-super-vl-120b-a12b"}
    for route, upstream in want.items():
        out = post(base, req(route))
        assert out["type"] == "message" and out["content"][0]["text"] == "ok", out
        call = last(up)[0]
        assert call["model"] == upstream, (route, call["model"])
        assert call["_auth"] == ("Bearer ds-test" if route.startswith("ds") else "Bearer nv-test")


def test_upstream_sees_normalized_request(gateway):
    base, up, _ = gateway
    post(base, req("ds-pro", messages=[{"role": "user", "content": [{"type": "text", "text": "look"}, IMG]}]))
    c = last(up)[0]
    assert c.get("thinking") == {"type": "enabled"}, c.keys()
    assert "output_config" not in c and IMAGE_NOTE in json.dumps(c["messages"])
    post(base, req("ds-flash", "medium"))
    assert last(up)[0].get("thinking") == {"type": "disabled"}
    post(base, req("nv-fast", "max"))
    c = last(up)[0]
    cap = c.get("max_tokens", c.get("max_completion_tokens"))   # LiteLLM sends the latter to NIM
    assert cap == 8192 and "thinking" not in c and "reasoning_effort" not in c


def test_tool_call_round_trip(gateway):
    base, up, _ = gateway
    tools = [{"name": "get_weather", "description": "w", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
    out = post(base, req("nv-smart", tools=tools, messages=[{"role": "user", "content": "CALLTOOL please"}]))
    tu = [b for b in out["content"] if b["type"] == "tool_use"]
    assert tu and tu[0]["name"] == "get_weather" and tu[0]["input"] == {"city": "Paris"}, out
    assert out["stop_reason"] == "tool_use"
    msgs = [{"role": "user", "content": "CALLTOOL please"},
            {"role": "assistant", "content": out["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu[0]["id"], "content": "sunny"}]}]
    out2 = post(base, req("nv-smart", tools=tools, messages=msgs))
    assert out2["content"][0]["text"] == "ok"
    assert any(m.get("role") == "tool" for m in last(up)[0]["messages"])


def test_streaming_is_anthropic_sse(gateway):
    base, up, _ = gateway
    raw = post(base, req("ds-flash"), stream=True)
    assert "message_start" in raw and "content_block_delta" in raw and "message_stop" in raw, raw[:500]


def test_free_tier_429_falls_back(gateway):
    base, up, _ = gateway
    n = len(up.calls)
    out = post(base, req("nv-smart", messages=[{"role": "user", "content": "FORCE429"}]))
    assert out["content"][0]["text"] == "ok"
    assert "z-ai/glm-5.3" in up.models()[n:], up.models()[n:]


def test_role_routes_resolve_through_the_profile(gateway):
    base, up, _ = gateway
    post(base, req("role-reviewer", "high"))
    c = last(up)[0]
    assert c["model"] == "deepseek-v4-pro" and c.get("thinking") == {"type": "enabled"}
    post(base, req("role-runner", "low"))
    assert last(up)[0]["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_unknown_role_fails_closed_without_calling_upstream(gateway):
    base, up, _ = gateway
    n = len(up.calls)
    with pytest.raises(AssertionError) as e:
        post(base, req("role-no-such-role"))
    assert "role-no-such-role" in str(e.value) and len(up.calls) == n


def test_probe_identity_runs_against_the_mock(gateway, tmp_path):
    base, up, _ = gateway
    env = {**os.environ, "DEEPSEEK_API_BASE": up.url, "DEEPSEEK_API_KEY": "ds-test", "PROBE_OUT": str(tmp_path / "i.json")}
    r = subprocess.run([sys.executable, str(GW / "probe.py"), "--identity"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads((tmp_path / "i.json").read_text())["identity"]
    assert rep["pro"]["answered"] == len(QUIZ) and rep["pro"]["models"] == ["deepseek-v4-pro"]
    sent = [c for c in up.calls if c["_path"].endswith("/chat/completions") and c["model"] in ("deepseek-v4-pro", "deepseek-flash")]
    assert all(c.get("thinking") == {"type": "enabled"} for c in sent[-2 * len(QUIZ):])


def test_one_gateway_serves_different_profiles_per_request(gateway):
    base, up, _ = gateway
    hp = lambda name: {PROFILE_HEADER: name}  # noqa: E731
    post(base, req("role-reviewer", "high"))
    assert last(up)[0]["model"] == "deepseek-v4-pro"                       # gateway default: baseline
    post(base, req("role-reviewer", "high"), headers=hp("flash-first"))
    assert last(up)[0]["model"] == "deepseek-flash"                        # same gateway, other profile
    post(base, req("role-implementer", "high"), headers=hp("thrift"))
    assert last(up)[0]["model"] == "deepseek-ai/deepseek-v4.1-flash"
    post(base, req("role-background", "low"), headers=hp("interactive"))
    assert last(up)[0]["model"] == "moonshotai/kimi-k2.6"
    post(base, req("role-background", "low"))
    assert last(up)[0]["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"   # no header: unchanged
    assert not [c for c in up.calls if PROFILE_HEADER in c.get("_headers", {})], "routing header leaked upstream"


def test_unknown_profile_header_fails_closed(gateway):
    base, up, _ = gateway
    n = len(up.calls)
    with pytest.raises(AssertionError) as e:
        post(base, req("role-lead"), headers={PROFILE_HEADER: "no-such-profile"})
    assert "not allowed" in str(e.value) and len(up.calls) == n


def test_route_log_records_profile_tier_and_served_model(gateway):
    base, up, _ = gateway
    post(base, req("role-triage", "low", messages=[{"role": "user", "content": "FORCE429"}]),
         headers={PROFILE_HEADER: "interactive"})
    time.sleep(1.0)   # success callbacks are async
    recs = routes(base)
    r = [x for x in recs if x.get("event") == "route" and x.get("profile") == "interactive" and x.get("requested") == "role-triage"]
    assert r and r[-1]["tier"] == "nv-smart", recs[-5:]
    s = [x for x in recs if x.get("event") == "served" and x.get("requested") == "role-triage"]
    assert s, recs[-5:]
    assert s[-1]["profile"] == "interactive" and "glm-5.3" in str(s[-1]["served_model"]), s[-1]
    assert s[-1]["fallback"] is True and s[-1]["served_tier"] == "nv-smart-alt", s[-1]


def test_allowed_profiles_limit_what_a_client_may_select(tmp_path):
    if not shutil.which("litellm"):
        pytest.skip("litellm not installed")
    up = Upstream()
    base, p, log, cfg = start_gateway(up, {"ALLOWED_PROFILES": "baseline,interactive"})
    try:
        post(base, req("role-lead"), headers={PROFILE_HEADER: "interactive"})
        with pytest.raises(AssertionError, match="not allowed"):
            post(base, req("role-lead"), headers={PROFILE_HEADER: "flash-first"})
    finally:
        stop_gateway(p, cfg)


def test_plug_in_any_openai_compatible_model(tmp_path):
    """The template's promise: add a tier block + point a role at it, and agents use the new model."""
    if not shutil.which("litellm"):
        pytest.skip("litellm not installed")
    up = Upstream()
    tiers = tmp_path / "tiers.toml"
    tiers.write_text((GW / "tiers.toml").read_text() + """
[tiers.local-coder]
model = "openai/qwen3.5-coder-32b"
api_base = "${LOCAL_API_BASE:-http://127.0.0.1:1/v1}"
api_key_env = ""
fallbacks = ["ds-flash"]
""")
    prof = tmp_path / "profiles"
    prof.mkdir()
    for f in profiles_dir().glob("*.toml"):
        (prof / f.name).write_text(f.read_text())
    (prof / "local.toml").write_text('extends = "baseline"\n[roles]\nimplementer = "local-coder"\n')
    base, p, log, cfg = start_gateway(up, {"TIERS_FILE": str(tiers), "PROFILES_DIR": str(prof),
                                           "PROFILE": "local", "LOCAL_API_BASE": up.url + "/v1"})
    try:
        out = post(base, req("role-implementer", "high"))
        assert out["content"][0]["text"] == "ok"
        c = last(up)[0]
        assert c["model"] == "qwen3.5-coder-32b" and c["_path"] == "/v1/chat/completions"
        assert c["_auth"] == "Bearer sk-none" and "thinking" not in c   # keyless, thinking = "none"
        post(base, req("role-reviewer", "high"))                         # other roles untouched
        assert last(up)[0]["model"] == "deepseek-v4-pro"
    finally:
        stop_gateway(p, cfg)


def test_probe_script_passes_against_the_mock(gateway, tmp_path):
    base, up, _ = gateway
    env = {**os.environ, "GATEWAY_PORT": base.rsplit(":", 1)[1], "GATEWAY_KEY": KEY, "PROBE_OUT": str(tmp_path / "p.json"),
           "DEEPSEEK_API_BASE": up.url, "NVIDIA_API_BASE": up.url + "/v1", "DEEPSEEK_API_KEY": "ds-test"}
    r = subprocess.run([sys.executable, str(GW / "probe.py")], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("PASS") == len(ROUTES) + 1, r.stdout   # every tier + one role route
    rep = json.loads((tmp_path / "p.json").read_text())
    assert set(rep["tiers"]) == set(ROUTES) | {"role-runner"}


# ------------------------------------------------------------------ 3. Claude Code through the gateway
def test_claude_code_routes_lead_and_subagent(gateway, tmp_path):
    if not shutil.which("claude"):
        pytest.skip("claude CLI not installed")
    base, up, log = gateway
    proj = tmp_path / "proj"
    (proj / ".claude" / "agents").mkdir(parents=True)
    (proj / ".claude" / "agents" / "probe.md").write_text(
        "---\nname: probe\ndescription: probe agent\ntools: Read\nmodel: role-triage\neffort: low\n---\nSay hi.\n")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))}
    env.update({"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": KEY,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "role-lead-plan", "ANTHROPIC_DEFAULT_SONNET_MODEL": "role-lead",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "role-background", "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    n = len(up.calls)
    r = subprocess.run(["claude", "-p", "go", "--model", "sonnet", "--dangerously-skip-permissions",
                        "--setting-sources", "project"], cwd=proj, env=env, capture_output=True, text=True,
                       timeout=180, stdin=subprocess.DEVNULL)
    seen = up.models()[n:]
    assert r.returncode == 0, (r.stdout[-1500:], r.stderr[-1500:], Path(log).read_text()[-2000:])
    assert "deepseek-flash" in seen, seen                 # lead: sonnet -> role-lead -> ds-flash
    assert "moonshotai/kimi-k2.6" in seen, seen           # subagent: role-triage -> nv-smart
    lead_calls = [c for c in up.calls[n:] if c["model"] == "deepseek-flash"]
    assert any(m.get("role") == "tool" for c in lead_calls for m in c["messages"]), "Agent tool result never came back"
