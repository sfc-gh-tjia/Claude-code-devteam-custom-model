#!/usr/bin/env python3
"""DevTeam referee: the deterministic half of the dev team.

Agents write code, tests and verdicts. The referee owns git, worktrees, freezing, gates,
integration, evidence and the ledger, and never trusts an agent's claim it can check itself.

Contract with the workflow:
- Every verb prints ONE short JSON line (at most ~900 bytes). The runner relays it verbatim.
  Details go to files under .loop/ and are handed on by path, never retyped by a model.
- Long verbs (check, integrate, gate) run as a detached job, so no call outlives the runner's
  10-minute Bash limit. The call waits up to budgets.bash_wait_s, then answers
  {"pending": true, "job": KEY}; `referee.py wait KEY` picks the job up again.
- The exit code is 0 unless --strict is given (make targets use --strict).

Stdlib only, Python 3.11+.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOP = ROOT / ".loop"
TAIL = 3000
LINE_MAX = 900
LONG_VERBS = {"check", "integrate", "gate"}
DEFAULT_PROTECTED = [".claude/**", "harness/**", ".git/**", ".loop/state/**", ".loop/jobs/**"]


class Fail(Exception):
    def __init__(self, code: str, msg: str = "", **extra):
        super().__init__(msg)
        self.code, self.msg, self.extra = code, msg, extra


# ---------------------------------------------------------------- config, tasks, state

_CFG = None


def cfg() -> dict:
    global _CFG
    if _CFG is None:
        c = tomllib.loads((ROOT / "harness" / "project.toml").read_text())
        p = c.setdefault("project", {})
        p.setdefault("main_branch", "main")
        p.setdefault("worktree_dir", ".worktrees")
        p.setdefault("test_cmd", "python3 -m pytest -q {tests}")
        p.setdefault("red_exit_codes", [1, 2])
        p.setdefault("scope_ignore", ["**/__pycache__/**", "*.pyc", ".pytest_cache/**", "**/.DS_Store"])
        b = c.setdefault("budgets", {})
        for k, v in {"max_parallel": 3, "attempts": 3, "review_rounds": 2,
                     "run_output_tokens": 3_000_000, "bash_wait_s": 540,
                     "integrate_lock_wait_s": 1800}.items():
            b.setdefault(k, v)
        for k in ("setup", "doctor", "gates"):
            c.setdefault(k, [])
        c.setdefault("lanes", {})
        c.setdefault("protected", {}).setdefault("paths", DEFAULT_PROTECTED)
        _CFG = c
    return _CFG


def rel(p: Path, base: Path = ROOT) -> str:
    try:
        return Path(p).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(p)


def match(path: str, pat: str) -> bool:
    """Glob match where a directory entry ('src/', 'src/**') also covers everything below it."""
    if fnmatch.fnmatchcase(path, pat):
        return True
    base = pat[:-3] if pat.endswith("/**") else pat.rstrip("/")
    return path == base or path.startswith(base + "/")


def tasks() -> dict:
    out = {}
    for p in sorted((ROOT / "work" / "tasks").glob("*.md")):
        if p.name.upper().startswith("TEMPLATE"):
            continue
        text = p.read_text()
        end = text.find("\n+++", 4)
        if not text.startswith("+++\n") or end < 0:
            raise Fail("TASK_FORMAT", f"{rel(p)}: missing +++ TOML front matter")
        try:
            m = tomllib.loads(text[4:end])
        except tomllib.TOMLDecodeError as e:
            raise Fail("TASK_FORMAT", f"{rel(p)}: {e}")
        m["id"], m["phase"] = str(m["id"]), str(m["phase"]).zfill(2)
        m["depends_on"] = [str(d) for d in m.get("depends_on", [])]
        m.setdefault("files", [])
        m.setdefault("tests", [])
        m.setdefault("title", p.stem)
        m["path"] = rel(p)
        if m["id"] in out:
            raise Fail("TASK_FORMAT", f"duplicate task id {m['id']}")
        out[m["id"]] = m
    return out


def task(tid: str) -> dict:
    t = tasks().get(tid)
    if not t:
        raise Fail("NO_SUCH_TASK", tid)
    return t


def lane_of(t: dict) -> str:
    if t.get("lane"):
        return t["lane"]
    lanes, fs = cfg()["lanes"], t["files"]
    if any(match(f, g) for f in fs for g in lanes.get("risk", [])):
        return "risk"
    if any(match(f, g) for f in fs for g in lanes.get("ui", [])):
        return "ui"
    if fs and all(any(match(f, g) for g in lanes.get("light", [])) for f in fs):
        return "light"
    return "default"


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(p)


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def state(tid: str) -> dict:
    return read_json(LOOP / "state" / f"{tid}.json", {})


def save_state(tid: str, s: dict) -> None:
    write_json(LOOP / "state" / f"{tid}.json", s)


def need_state(tid: str, ok: set) -> dict:
    s = state(tid)
    if s.get("status") not in ok:
        raise Fail("BAD_STAGE", f"task {tid} is {s.get('status') or 'not prepared'}; expected {sorted(ok)}")
    return s


def runs(tid: str) -> Path:
    d = LOOP / "runs" / tid
    d.mkdir(parents=True, exist_ok=True)
    return d


def main_branch() -> str:
    return cfg()["project"]["main_branch"]


def wt_root() -> Path:
    return ROOT / cfg()["project"]["worktree_dir"]


def wt(tid: str) -> Path:
    return wt_root() / f"task-{tid}"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def expand(entries: list, base: Path) -> list:
    """Declared paths (files, dirs or globs) -> existing files, relative to base."""
    out = set()
    for e in entries:
        p = base / e
        if p.is_file():
            out.add(e)
        elif p.is_dir():
            out.update(rel(x, base) for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts)
        else:
            out.update(rel(x, base) for x in base.glob(e) if x.is_file())
    return sorted(out)


# ---------------------------------------------------------------- processes

def git_run(*a, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def git(*a, cwd: Path = ROOT) -> str:
    r = git_run(*a, cwd=cwd)
    if r.returncode:
        raise Fail("GIT_ERROR", f"git {' '.join(a)}: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def dirty(cwd: Path = ROOT) -> bool:
    return bool(git("status", "--porcelain", "--untracked-files=no", cwd=cwd))


def sh(cmd: str, cwd: Path, timeout: float, log: Path) -> dict:
    """Run a shell command with a hard timeout (its whole process group is killed on expiry)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # No bytecode cache: a retry that rewrites a module with the same size within the same second
    # would otherwise run the stale .pyc (mtime has 1 s resolution) and fail correct code.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    with open(log, "w") as f:
        p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                             start_new_session=True, executable="/bin/bash", env=env)
        try:
            rc, timed_out = p.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            rc, timed_out = -9, True
    out = log.read_text(errors="replace")
    if timed_out:
        out += f"\n[referee] TIMEOUT after {timeout}s: {cmd}\n"
    return {"rc": rc, "timed_out": timed_out, "secs": round(time.time() - t0, 1),
            "tail": out[-TAIL:], "log": rel(log)}


def gates_for(lane: str | None, stage: str) -> list:
    """stage: check (task tree), integrate (scratch merge), smoke (main after ff), phase (main)."""
    out = []
    for g in cfg()["gates"]:
        lanes = g.get("lanes", ["*"])
        if lane and "*" not in lanes and lane not in lanes:
            continue
        if stage == "smoke" and not g.get("smoke"):
            continue
        if stage != "smoke" and g.get(stage) is False:
            continue
        out.append(g)
    return out


def run_gates(gs: list, cwd: Path, logdir: Path) -> list:
    """Run gates in config order (cheap first) and stop at the first red one."""
    res = []
    for g in gs:
        r = sh(g["cmd"], cwd, g.get("timeout", 900), logdir / f"{g['id']}.log")
        r.update(id=g["id"], passed=r["rc"] == 0)
        res.append(r)
        if not r["passed"]:
            break
    return res


def last_line(s: str) -> str:
    lines = [x for x in s.strip().splitlines() if x.strip()]
    return lines[-1][:200] if lines else ""


@contextlib.contextmanager
def integrate_lock():
    LOOP.mkdir(exist_ok=True)
    f = open(LOOP / "integrate.lock", "a+")
    deadline = time.time() + cfg()["budgets"]["integrate_lock_wait_s"]
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released by the OS if we die
            break
        except BlockingIOError:
            if time.time() > deadline:
                f.close()
                raise Fail("INTEGRATE_BUSY", "another integrate held the lock too long")
            time.sleep(2)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


# ---------------------------------------------------------------- verbs

def v_doctor(a) -> dict:
    fails = []
    wd = cfg()["project"]["worktree_dir"]
    if any(match(f"{wd}/x", g) for g in cfg()["protected"]["paths"]):
        fails.append(f"worktree_dir '{wd}' is inside a protected path; agents could not write there")
    try:
        br = git("rev-parse", "--abbrev-ref", "HEAD")
        if br != main_branch():
            fails.append(f"repo is on '{br}', expected '{main_branch()}' (integrate fast-forwards it)")
        elif dirty():
            fails.append("main checkout has uncommitted tracked changes (integrate fast-forwards it)")
        anchor = wt_root() / ".anchor"
        if not anchor.exists():
            # A locked anchor keeps .git/worktrees alive: some sandboxes cannot recreate it.
            git("worktree", "add", "--detach", str(anchor), main_branch())
            git("worktree", "lock", str(anchor))
    except Fail as e:
        fails.append(e.msg)
    for d in cfg()["doctor"]:
        name = d.get("name") or d["cmd"].split()[0]
        r = sh(d["cmd"], ROOT, d.get("timeout", 120), LOOP / "doctor" / f"{name}.log")
        if r["rc"]:
            fails.append(f"{name}: rc={r['rc']} {last_line(r['tail'])}")
    try:
        tasks()
    except Fail as e:
        fails.append(e.msg)
    return {"passed": not fails, "code": "DOCTOR_FAILED" if fails else None, "failures": fails[:8]}


def _ids(s: str | None) -> set:
    return set(filter(None, (s or "").split(",")))


def v_plan(a) -> dict:
    P, T = a.phase.zfill(2), tasks()
    only, excl, busy = _ids(a.only), _ids(a.exclude), _ids(a.busy)
    ph = sorted((t for t in T.values() if t["phase"] == P and (not only or t["id"] in only)),
                key=lambda t: t["id"])
    if not ph:
        raise Fail("NO_TASKS", f"no tasks in phase {P}")
    st = {i: state(i).get("status") for i in T}
    claimed = set()
    for i in busy:
        if i in T:
            claimed |= set(T[i]["files"]) | set(T[i]["tests"])
    ready, waiting = [], []
    for t in ph:
        i = t["id"]
        if st[i] in ("merged", "blocked") or i in excl or i in busy:
            continue
        paths = set(t["files"]) | set(t["tests"])
        deps_open = [d for d in t["depends_on"] if st.get(d) != "merged"]
        if deps_open or any(match(p, q) or match(q, p) for p in paths for q in claimed):
            waiting.append(i)
            continue
        claimed |= paths
        ready.append({"id": i, "lane": lane_of(t)})
    key = hashlib.sha256((git("rev-parse", main_branch()) + "".join(
        (ROOT / t["path"]).read_text() for t in ph)).encode()).hexdigest()[:16]
    pc = read_json(LOOP / "precheck" / f"{P}.json", {})
    b = cfg()["budgets"]
    return {"passed": True, "phase": P, "ready": ready, "waiting": waiting,
            "blocked": [{"id": t["id"], "code": state(t["id"]).get("code")} for t in ph if st[t["id"]] == "blocked"],
            "merged": sum(st[t["id"]] == "merged" for t in ph), "total": len(ph),
            "precheck_key": key, "precheck_needed": not (pc.get("key") == key and not pc.get("blocking")),
            "budgets": {k: b[k] for k in ("max_parallel", "attempts", "review_rounds", "run_output_tokens")},
            "worktree_dir": cfg()["project"]["worktree_dir"]}


def v_prepare(a) -> dict:
    t, s, w = task(a.id), state(a.id), wt(a.id)
    if s.get("status") == "merged":
        raise Fail("ALREADY_MERGED", a.id)
    if s.get("status") == "blocked":
        raise Fail("BLOCKED", f"{a.id} is blocked ({s.get('code')}); a human runs `referee.py unblock {a.id}`")
    if not w.exists():
        if git("branch", "--list", f"task/{a.id}"):
            git("worktree", "add", str(w), f"task/{a.id}")
        else:
            git("worktree", "add", "-b", f"task/{a.id}", str(w), main_branch())
    if not s:
        s = {"id": a.id, "status": "prepared", "lane": lane_of(t), "frozen": {}, "history": []}
    if not s.get("setup_ok"):
        for i, st in enumerate(cfg()["setup"]):
            r = sh(st["cmd"], w, st.get("timeout", 600), runs(a.id) / f"setup-{i}.log")
            if r["rc"]:
                save_state(a.id, s)
                raise Fail("ENV_SETUP", f"setup '{st['cmd']}' rc={r['rc']}: {last_line(r['tail'])}", log=r["log"])
        s["setup_ok"] = True
    save_state(a.id, s)
    return {"passed": True, "status": s["status"], "lane": s["lane"], "worktree": rel(w)}


def v_freeze(a) -> dict:
    t, s, w = task(a.id), need_state(a.id, {"prepared", "frozen"}), wt(a.id)
    if s["status"] == "frozen":
        return {"passed": True, "already": True}
    files = expand(t["tests"], w)
    if t["tests"] and not files:
        return {"passed": False, "code": "TESTS_MISSING", "msg": "no files at the task's declared test paths"}
    stray = [x[3:] for x in git("status", "--porcelain", "--untracked-files=all", cwd=w).splitlines()
             if not any(match(x[3:], e) for e in t["tests"] + cfg()["project"]["scope_ignore"])]
    if stray:
        return {"passed": False, "code": "SCOPE_VIOLATION", "msg": "QA changed non-test paths", "paths": stray[:5]}
    if files:
        cmd = cfg()["project"]["test_cmd"].format(tests=" ".join(shlex.quote(f) for f in files))
        r = sh(cmd, w, 900, runs(a.id) / "red-check.log")
        if r["rc"] not in cfg()["project"]["red_exit_codes"]:
            code = "TESTS_NOT_RED" if r["rc"] == 0 else "TESTS_BROKEN"
            write_json(runs(a.id) / "freeze.json", {"code": code, **r})
            return {"passed": False, "code": code, "detail": rel(runs(a.id) / "freeze.json")}
        git("add", "--", *files, cwd=w)
        if git_run("diff", "--cached", "--quiet", cwd=w).returncode:
            git("commit", "-q", "-m", f"test({a.id}): frozen acceptance tests", cwd=w)
    s.update(status="frozen", frozen={f: sha256(w / f) for f in files}, freeze_sha=git("rev-parse", "HEAD", cwd=w))
    save_state(a.id, s)
    return {"passed": True, "frozen": len(files)}


def v_refreeze(a) -> dict:
    """Human-only: record a human-approved amendment of a task's frozen tests."""
    t, s, w = task(a.id), need_state(a.id, {"frozen", "blocked"}), wt(a.id)
    files = expand(t["tests"], w)
    git("add", "--", *files, cwd=w)
    if git_run("diff", "--cached", "--quiet", cwd=w).returncode:
        git("commit", "-q", "-m", f"test({a.id}): approved amendment of frozen tests", cwd=w)
    s.update(frozen={f: sha256(w / f) for f in files}, freeze_sha=git("rev-parse", "HEAD", cwd=w))
    if s["status"] == "blocked":
        s["status"] = "frozen"
        s.pop("code", None)
    save_state(a.id, s)
    return {"passed": True, "frozen": len(files)}


def v_check(a) -> dict:
    t, s, w, rd = task(a.id), need_state(a.id, {"frozen"}), wt(a.id), runs(a.id)
    out = {"id": a.id, "attempt": len(s["history"]) + 1}
    touched = [f for f, h in s["frozen"].items() if not (w / f).exists() or sha256(w / f) != h]
    if touched:  # self-heal: restore the oracle, discard the edits, tell the implementer
        git("checkout", s["freeze_sha"], "--", *touched, cwd=w)
    git("add", "-A", cwd=w)
    if git_run("diff", "--cached", "--quiet", cwd=w).returncode:
        git("commit", "-q", "-m", f"wip({a.id}): check {out['attempt']}", cwd=w)
    base = git("merge-base", main_branch(), "HEAD", cwd=w)
    changed = git("diff", "--name-only", base, "HEAD", cwd=w).splitlines()
    (rd / "diff.patch").write_text(git("diff", base, "HEAD", cwd=w))
    allowed = t["files"] + t["tests"] + [f"work/evidence/{a.id}.json"] + cfg()["project"]["scope_ignore"]
    out["out_of_scope"] = [c for c in changed if not any(match(c, p) for p in allowed)]
    out["protected"] = [c for c in changed if any(match(c, g) for g in cfg()["protected"]["paths"])]
    out["gates"] = []
    if touched:
        code = "FROZEN_MODIFIED"
    elif out["protected"]:
        code = "CONTROL_PLANE_VIOLATION"
    elif out["out_of_scope"]:
        code = "SCOPE_VIOLATION"
    else:
        own = expand(t["tests"], w)
        gs = ([{"id": "task-tests", "timeout": 900, "cmd": cfg()["project"]["test_cmd"].format(
            tests=" ".join(shlex.quote(f) for f in own))}] if own else []) + gates_for(s["lane"], "check")
        out["gates"] = run_gates(gs, w, rd / "check")
        code = None if all(g["passed"] for g in out["gates"]) else "GATES_RED"
    out.update(code=code, passed=code is None, frozen_restored=touched, sha=git("rev-parse", "HEAD", cwd=w))
    write_json(rd / "check.json", out)
    s["history"].append({"code": code, "failing": [g["id"] for g in out["gates"] if not g["passed"]], "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    s["last_check"] = {"sha": out["sha"], "passed": out["passed"]}
    save_state(a.id, s)
    return {"passed": out["passed"], "code": code, "failing": s["history"][-1]["failing"],
            "out_of_scope": out["out_of_scope"][:5], "frozen_restored": touched[:5], "detail": rel(rd / "check.json")}


def evidence(tid: str, s: dict) -> dict:
    rd = runs(tid)
    verdicts = {p.stem: read_json(p) for p in sorted(rd.glob("*-r*.json"))}
    return {"id": tid, "lane": s["lane"], "checks": s["history"], "verdicts": verdicts,
            "frozen_tests": sorted(s["frozen"]), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def v_integrate(a) -> dict:
    t, s, w, rd = task(a.id), need_state(a.id, {"frozen"}), wt(a.id), runs(a.id)
    lc = s.get("last_check") or {}
    if not (lc.get("passed") and lc.get("sha") == git("rev-parse", "HEAD", cwd=w)):
        raise Fail("NOT_CHECKED", "run `check` on the task's current HEAD first")
    with integrate_lock():
        ev = w / "work" / "evidence" / f"{a.id}.json"
        write_json(ev, evidence(a.id, s))
        git("add", "--", rel(ev, w), cwd=w)
        git("commit", "-q", "-m", f"chore({a.id}): evidence", cwd=w)
        scratch = wt_root() / f"integ-{a.id}"
        if scratch.exists():
            git_run("worktree", "remove", "--force", str(scratch))
        git("worktree", "add", "--detach", str(scratch), main_branch())
        try:
            m = git_run("merge", "--no-ff", "--no-edit", "-m", f"merge({a.id}): {t['title']}", f"task/{a.id}", cwd=scratch)
            if m.returncode:
                git_run("merge", "--abort", cwd=scratch)
                return {"passed": False, "code": "MERGE_CONFLICT", "msg": last_line(m.stdout + m.stderr)}
            for i, st in enumerate(x for x in cfg()["setup"] if x.get("scratch", True)):
                r = sh(st["cmd"], scratch, st.get("timeout", 600), rd / "integrate" / f"setup-{i}.log")
                if r["rc"]:
                    return {"passed": False, "code": "ENV_SETUP", "msg": last_line(r["tail"])}
            gr = run_gates(gates_for(None, "integrate"), scratch, rd / "integrate")
            write_json(rd / "integrate.json", {"gates": gr})
            if not all(g["passed"] for g in gr):
                return {"passed": False, "code": "GATES_RED_INTEGRATE",
                        "failing": [g["id"] for g in gr if not g["passed"]], "detail": rel(rd / "integrate.json")}
            new = git("rev-parse", "HEAD", cwd=scratch)
        finally:
            git_run("worktree", "remove", "--force", str(scratch))
        if git("rev-parse", "--abbrev-ref", "HEAD") != main_branch() or dirty():
            raise Fail("MAIN_NOT_CLEAN", "main checkout must be on the main branch and clean")
        git("merge", "--ff-only", "-q", new)
        smoke = run_gates(gates_for(None, "smoke"), ROOT, rd / "smoke")
        if not all(g["passed"] for g in smoke):
            git("revert", "-m", "1", "--no-edit", new)
            s.update(status="blocked", code="MAIN_RED_AFTER_MERGE", prev_status="frozen")
            save_state(a.id, s)
            return {"passed": False, "code": "MAIN_RED_AFTER_MERGE", "reverted": new[:10],
                    "failing": [g["id"] for g in smoke if not g["passed"]]}
        s.update(status="merged", merged_sha=new, merged_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        save_state(a.id, s)
    return {"passed": True, "merged": new[:10]}


def v_block(a) -> dict:
    s = state(a.id) or {"id": a.id, "frozen": {}, "history": []}
    if s.get("status") == "merged":
        return {"passed": True, "note": "already merged"}
    if s.get("status") != "blocked":
        s["prev_status"] = s.get("status")
    s.update(status="blocked", code=a.code, blocked_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    save_state(a.id, s)
    return {"passed": True, "blocked": a.id, "code": a.code}


def v_unblock(a) -> dict:
    s = need_state(a.id, {"blocked"})
    s["status"] = s.pop("prev_status", None) or "prepared"
    s.pop("code", None)
    save_state(a.id, s)
    return {"passed": True, "status": s["status"]}


def v_reset(a) -> dict:
    """Human-only: throw a task's attempt away (worktree, branch, state, runs)."""
    w = wt(a.id)
    if w.exists():
        git_run("worktree", "remove", "--force", str(w))
    git_run("branch", "-D", f"task/{a.id}")
    (LOOP / "state" / f"{a.id}.json").unlink(missing_ok=True)
    for p in sorted((LOOP / "runs" / a.id).rglob("*"), reverse=True) if (LOOP / "runs" / a.id).exists() else []:
        p.unlink() if p.is_file() else p.rmdir()
    return {"passed": True, "reset": a.id}


def v_gate(a) -> dict:
    T = tasks()
    if a.phase:
        P = a.phase.zfill(2)
        unmerged = [i for i, t in T.items() if t["phase"] == P and state(i).get("status") != "merged"]
    else:
        P, unmerged = "all", []
    gr = run_gates(gates_for(None, "phase"), ROOT, LOOP / "gate" / P)
    out = {"phase": P, "unmerged": unmerged, "gates": gr,
           "passed": not unmerged and all(g["passed"] for g in gr)}
    write_json(LOOP / f"phase-{P}-gate.json", out)
    return {"passed": out["passed"], "unmerged": unmerged[:20],
            "failing": [g["id"] for g in gr if not g["passed"]], "detail": rel(LOOP / f"phase-{P}-gate.json")}


def v_status(a) -> dict:
    by = {}
    for i, t in tasks().items():
        if a.phase and t["phase"] != a.phase.zfill(2):
            continue
        s = state(i)
        st = s.get("status") or "todo"
        by.setdefault(t["phase"], {}).setdefault(st, []).append(i + (f":{s['code']}" if s.get("code") else ""))
    return {"passed": True, "phases": by}


def v_recover(a) -> dict:
    removed = []
    if wt_root().exists():
        for p in wt_root().glob("integ-*"):
            git_run("worktree", "remove", "--force", str(p))
            removed.append(p.name)
    git_run("worktree", "prune")
    dead = []
    for jf in (LOOP / "jobs").glob("*.json") if (LOOP / "jobs").exists() else []:
        if jf.name.endswith(".result.json"):
            continue
        job = read_json(jf, {})
        if job and not alive(job.get("pid", 0)) and not job_result(job):
            jf.unlink()
            dead.append(job.get("key"))
    return {"passed": True, "removed_scratch": removed, "dead_jobs": dead}


def v_reap(a) -> dict:
    trees = branches = logs = 0
    for i in tasks():
        if state(i).get("status") != "merged":
            continue
        if wt(i).exists():
            git_run("worktree", "remove", "--force", str(wt(i)))
            trees += 1
        if git_run("branch", "-d", f"task/{i}").returncode == 0:
            branches += 1
        for p in (LOOP / "runs" / i).rglob("*.log") if (LOOP / "runs" / i).exists() else []:
            p.unlink()
            logs += 1
    git_run("worktree", "prune")
    return {"passed": True, "worktrees": trees, "branches": branches, "logs": logs}


def v_ledger(a) -> dict:
    T = tasks()
    rows = ["| Task | Phase | Lane | Status | Checks | Merged | Title |", "|---|---|---|---|---|---|---|"]
    for i, t in sorted(T.items()):
        s = state(i)
        st = s.get("status") or "todo"
        if s.get("code"):
            st += f" ({s['code']})"
        rows.append(f"| {i} | {t['phase']} | {s.get('lane') or lane_of(t)} | {st} | {len(s.get('history', []))} "
                    f"| {(s.get('merged_sha') or '')[:10]} | {t['title']} |")
    lines = ["# Ledger", "", "Generated by `harness/referee.py ledger` from `.loop/state`. Do not edit by hand.", "", *rows]
    for rv in sorted((LOOP / "runs").glob("phase-*/review.json")) if (LOOP / "runs").exists() else []:
        r = read_json(rv, {})
        lines += ["", f"## {rv.parent.name} review", ""]
        lines += [f"- follow-up: **{f.get('title')}** — {f.get('why')}" for f in r.get("follow_ups", [])]
        lines += [f"- learning: {x}" for x in r.get("learnings", [])]
    tel = (LOOP / "telemetry.jsonl")
    if tel.exists() and tel.read_text().strip():
        last = json.loads(tel.read_text().strip().splitlines()[-1])
        lines += ["", "## Latest telemetry", "", "```", json.dumps(last.get("summary", last), indent=1), "```"]
    p = ROOT / "work" / "ledger.md"
    p.write_text("\n".join(lines) + "\n")
    return {"passed": True, "ledger": rel(p), "tasks": len(T)}


def v_telemetry(a) -> dict:
    r = subprocess.run([sys.executable, str(ROOT / "harness" / "tokens.py"), "--project", str(ROOT), "--json"],
                       capture_output=True, text=True)
    if r.returncode:
        raise Fail("TELEMETRY_FAILED", last_line(r.stderr))
    rec = json.loads(r.stdout)
    LOOP.mkdir(exist_ok=True)
    with open(LOOP / "telemetry.jsonl", "a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")
    return {"passed": True, "summary": rec.get("summary")}


# ---------------------------------------------------------------- detached jobs

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def job_result(job: dict):
    r = read_json(LOOP / "jobs" / f"{job['key']}.result.json", {})
    return r.get("result") if r.get("token") == job.get("token") else None


def await_job(job: dict) -> dict:
    deadline = time.time() + cfg()["budgets"]["bash_wait_s"]
    while True:
        res = job_result(job)
        if res is not None:
            return res
        if not alive(job["pid"]):
            res = job_result(job)
            return res if res is not None else {"passed": False, "code": "JOB_DIED",
                                                "detail": rel(LOOP / "jobs" / f"{job['key']}.log")}
        if time.time() > deadline:
            return {"passed": True, "pending": True, "job": job["key"]}
        time.sleep(1)


def start_or_attach(verb: str, argv: list) -> dict:
    key = "-".join([verb] + [x for x in argv[1:] if not x.startswith("-")])
    jd = LOOP / "jobs"
    jd.mkdir(parents=True, exist_ok=True)
    job = read_json(jd / f"{key}.json")
    if not (job and alive(job["pid"]) and job_result(job) is None):  # nothing running: start fresh
        token = secrets.token_hex(4)
        with open(jd / f"{key}.log", "w") as log:
            p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_job", token, key, *argv],
                                 cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        job = {"key": key, "token": token, "pid": p.pid, "argv": argv}
        write_json(jd / f"{key}.json", job)
    return await_job(job)


def v_wait(a) -> dict:
    job = read_json(LOOP / "jobs" / f"{a.key}.json")
    if not job:
        raise Fail("NO_SUCH_JOB", a.key)
    return await_job(job)


# ---------------------------------------------------------------- entry point

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="referee.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--strict", action="store_true", help="exit 1 when the result did not pass")
    ap.add_argument("--inline", action="store_true", help="run long verbs in this process (no job)")
    sub = ap.add_subparsers(dest="verb", required=True)
    for name in ("doctor", "recover", "reap", "ledger", "telemetry"):
        sub.add_parser(name)
    p = sub.add_parser("plan")
    p.add_argument("phase")
    p.add_argument("--only")
    p.add_argument("--exclude")
    p.add_argument("--busy")
    for name in ("prepare", "freeze", "refreeze", "check", "integrate", "unblock", "reset"):
        sub.add_parser(name).add_argument("id")
    p = sub.add_parser("block")
    p.add_argument("id")
    p.add_argument("code")
    sub.add_parser("gate").add_argument("phase", nargs="?")
    sub.add_parser("status").add_argument("--phase")
    sub.add_parser("wait").add_argument("key")
    return ap


def dispatch(a) -> dict:
    fn = globals()[f"v_{a.verb}"]
    try:
        return fn(a)
    except Fail as e:
        return {"passed": False, "code": e.code, "msg": e.msg, **e.extra}
    except Exception as e:  # never let a traceback reach the runner as unparsable output
        return {"passed": False, "code": "INTERNAL_ERROR", "msg": repr(e)[:300]}


def emit(verb: str, res: dict) -> None:
    line = json.dumps(res, separators=(",", ":"), default=str)
    if len(line) > LINE_MAX:  # keep the relay tiny; the full result lives in a file
        full = LOOP / f"last-{verb}.json"
        write_json(full, res)
        keep = {k: res[k] for k in ("passed", "code", "pending", "job", "msg") if k in res}
        keep["detail"] = res.get("detail") or rel(full)
        line = json.dumps(keep, separators=(",", ":"), default=str)[:LINE_MAX]
    print(line)


def main(argv: list) -> int:
    if argv[:1] == ["_job"]:  # detached worker: _job TOKEN KEY <original argv...>
        token, key, rest = argv[1], argv[2], argv[3:]
        a = parser().parse_args(rest)
        write_json(LOOP / "jobs" / f"{key}.result.json", {"token": token, "result": dispatch(a)})
        return 0
    a = parser().parse_args(argv)
    try:
        if a.verb in LONG_VERBS and not a.inline and os.environ.get("DEVTEAM_INLINE") != "1":
            res = start_or_attach(a.verb, [x for x in argv if x not in ("--strict",)])
        else:
            res = dispatch(a)
    except Fail as e:  # config errors etc. raised before dispatch
        res = {"passed": False, "code": e.code, "msg": e.msg}
    except Exception as e:
        res = {"passed": False, "code": "INTERNAL_ERROR", "msg": repr(e)[:300]}
    emit(a.verb, res)
    return 1 if a.strict and not res.get("passed") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
