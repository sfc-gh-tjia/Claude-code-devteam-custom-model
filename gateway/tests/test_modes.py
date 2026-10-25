"""The two ways to use the shared gateway, no API keys needed:
standalone `claude-custom` (any repo, no Anthropic account) and DevTeam projects (installer, doctor).
Reuses the mock upstream and the real-LiteLLM gateway from test_gateway.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from test_gateway import GW, KEY, Upstream, gateway, routes, start_gateway, stop_gateway  # noqa: F401

ROOT = GW.parent
sys.path[:0] = [str(ROOT / "harness")]
import gateway_check  # noqa: E402
from routes import summarize  # noqa: E402
from tiers import load_tiers  # noqa: E402


def project(tmp_path: Path, base: str, token: str = KEY, profile: str | None = "baseline") -> Path:
    p = tmp_path / "proj"
    (p / ".claude").mkdir(parents=True)
    env = {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": token}
    if profile:
        env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-devteam-profile: {profile}"
    (p / ".claude" / "settings.json").write_text(json.dumps({"env": env}))
    return p


# ------------------------------------------------------------------ doctor's gateway check
def test_gateway_check_passes_on_a_devteam_gateway(gateway, tmp_path):
    base, up, _ = gateway
    res = gateway_check.check(project(tmp_path, base))
    assert all(ok for ok, _ in res) and len(res) == 3, res
    assert "profile baseline available" in res[1][1]


def test_gateway_check_rejects_wrong_token_and_unknown_profile(gateway, tmp_path):
    base, up, _ = gateway
    res = gateway_check.check(project(tmp_path / "a", base, token="sk-wrong"), no_call=True)
    assert not res[-1][0] and "token" in res[-1][1], res
    res = gateway_check.check(project(tmp_path / "b", base, profile="no-such-profile"), no_call=True)
    assert not res[-1][0] and "not available" in res[-1][1], res


def test_gateway_check_rejects_a_service_that_is_not_ours(tmp_path):
    class Imposter(BaseHTTPRequestHandler):   # healthy on /health, answers everything else with 200
        def log_message(self, *a):
            pass

        def _ok(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"type": "message", "content": []}')
        do_GET = do_POST = _ok
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Imposter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = gateway_check.check(project(tmp_path, f"http://127.0.0.1:{srv.server_address[1]}"))
        assert res[0][0] and not res[-1][0] and "not a DevTeam gateway" in res[-1][1], res
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ standalone: claude-custom
def test_claude_custom_works_with_no_anthropic_account(gateway, tmp_path):
    """Empty HOME and Claude config (no login), shell polluted with a fake Anthropic key and another
    endpoint: claude-custom must drop those, connect through the gateway with the interactive profile,
    and Claude Code's helper slot (haiku) must land on interactive's background tier."""
    if not shutil.which("claude"):
        pytest.skip("claude CLI not installed")
    base, up, _ = gateway
    home = tmp_path / "home"
    proj = home / "proj"
    (proj / ".claude" / "agents").mkdir(parents=True)
    (proj / ".claude" / "agents" / "probe.md").write_text(
        "---\nname: probe\ndescription: probe agent\ntools: Read\nmodel: haiku\n---\nSay hi.\n")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
    env.update({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "GATEWAY_PORT": base.rsplit(":", 1)[1], "GATEWAY_KEY": KEY,
                "ANTHROPIC_API_KEY": "sk-ant-should-never-be-sent", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/elsewhere"})
    n, logged = len(up.calls), len(routes(base))
    r = subprocess.run([str(ROOT / "standalone" / "claude-custom"), "-p", "go", "--dangerously-skip-permissions"],
                       cwd=proj, env=env, capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, (r.stdout[-800:], r.stderr[-1500:])
    assert "profile interactive" in r.stderr and "DeepSeek API" in r.stderr     # the banner says where code goes
    seen = up.models()[n:]
    assert "deepseek-flash" in seen and "moonshotai/kimi-k2.6" in seen, seen   # lead; helper slot -> nv-smart
    assert "nvidia/nemotron-3.5-lightning-30b-a3b" not in seen, seen          # not baseline's background
    profs = {x.get("profile") for x in routes(base)[logged:] if x.get("event") == "route"}
    assert profs == {"interactive"}, profs
    assert not [c for c in up.calls[n:] if "sk-ant-should-never-be-sent" in json.dumps(c.get("_headers"))]


def test_claude_custom_selfcheck(gateway, tmp_path):
    if not shutil.which("claude"):
        pytest.skip("claude CLI not installed")
    base, up, _ = gateway
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
    env.update({"GATEWAY_PORT": base.rsplit(":", 1)[1], "GATEWAY_KEY": KEY})
    r = subprocess.run([str(ROOT / "standalone" / "claude-custom"), "--selfcheck"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and r.stdout.startswith("PASS"), (r.stdout, r.stderr[-1000:])


def test_claude_custom_refuses_an_unknown_profile_before_starting_claude(tmp_path):
    r = subprocess.run([str(ROOT / "standalone" / "claude-custom"), "--profile", "nope", "--dry-run"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "claude-custom: no profile 'nope'" in r.stderr


# ------------------------------------------------------------------ DevTeam projects: one shared gateway
def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def test_install_uses_the_shared_gateway_by_default(tmp_path):
    dst = git_repo(tmp_path / "app")
    dry = subprocess.run([str(ROOT / "install.sh"), str(dst)], capture_output=True, text=True)
    assert dry.returncode == 0 and "copy: harness/referee.py" in dry.stdout
    assert "copy: gateway/" not in dry.stdout and "standalone/" not in dry.stdout
    subprocess.run([str(ROOT / "install.sh"), str(dst), "--apply"], check=True, capture_output=True)
    assert not (dst / "gateway").exists() and (dst / ".devteam-home").read_text().strip() == str(ROOT)
    assert "x-devteam-profile: baseline" in (dst / ".claude" / "settings.json").read_text()
    n = subprocess.run(["make", "-n", "probe", "routes"], cwd=dst, capture_output=True, text=True)
    assert f"{ROOT}/gateway/probe.py" in n.stdout and f"{ROOT}/gateway/routes.py" in n.stdout, n.stdout


def test_install_warns_about_an_old_gateway_copy_and_supports_with_gateway(tmp_path):
    old = git_repo(tmp_path / "old")
    (old / "gateway").mkdir()
    (old / "gateway" / "tiers.toml").write_text("# an older copy\n")
    r = subprocess.run([str(ROOT / "install.sh"), str(old)], capture_output=True, text=True)
    assert "already has its own gateway/" in r.stderr and (old / "gateway" / "tiers.toml").exists()
    own = git_repo(tmp_path / "own")
    subprocess.run([str(ROOT / "install.sh"), str(own), "--apply", "--with-gateway"], check=True, capture_output=True)
    assert (own / "gateway" / "tiers.toml").exists() and not (own / ".devteam-home").exists()
    assert not (own / "gateway" / ".env").exists() and not (own / "standalone").exists()


# ------------------------------------------------------------------ metrics and service
def test_routes_summary_counts_fallbacks_failures_and_cost():
    tiers = load_tiers(env={})
    lines = [
        {"event": "route", "id": "1", "profile": "baseline", "requested": "role-reviewer", "tier": "ds-pro"},
        {"event": "served", "id": "1", "served_tier": "ds-pro", "input_tokens": 1_000_000, "output_tokens": 100_000, "seconds": 2},
        {"event": "route", "id": "2", "profile": "interactive", "requested": "role-background", "tier": "nv-smart"},
        {"event": "failed", "id": "2", "error": "429"},
        {"event": "served", "id": "2", "served_tier": "ds-flash", "fallback": True, "input_tokens": 1_000_000, "output_tokens": 0, "seconds": 1},
        {"event": "route", "id": "3", "profile": "interactive", "requested": "role-background", "tier": "nv-smart"},
        {"event": "failed", "id": "3", "error": "boom"},
    ]
    rows = {(r["profile"], r["role"]): r for r in summarize(lines, tiers)}
    pro = rows[("baseline", "role-reviewer")]
    assert pro["requests"] == 1 and abs(pro["usd"] - (1.32 + 0.396)) < 1e-6
    bg = rows[("interactive", "role-background")]
    assert bg["requests"] == 2 and bg["fallbacks"] == 1 and bg["failed"] == 1 and abs(bg["usd"] - 0.30) < 1e-6


@pytest.mark.skipif(not shutil.which("plutil"), reason="macOS only")
def test_macos_service_plist_is_valid():
    out = subprocess.run([str(GW / "service" / "macos.sh"), "print"], capture_output=True, text=True, check=True).stdout
    f = Path(tempfile.mktemp(suffix=".plist"))
    f.write_text(out)
    assert subprocess.run(["plutil", "-lint", str(f)], capture_output=True).returncode == 0
    assert f"{GW}/start.sh" in out and "KeepAlive" in out
