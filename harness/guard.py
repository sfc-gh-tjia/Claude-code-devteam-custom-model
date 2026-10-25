#!/usr/bin/env python3
"""DevTeam guard: a PreToolUse hook that enforces each role's permissions in config, not in prompts.

Wired from each agent's frontmatter (`guard.py --role implementer`) and from .claude/settings.json
for the lead (`--role lead`). Reads the hook event JSON on stdin and answers with a
permissionDecision (deny or ask); silence means "no objection" (normal permissions still apply).

Design rules learned from earlier harnesses:
- Parse shell with shlex, not regexes, so ordinary read-only idioms (pipes, quoted '\\|' greps,
  heredocs fed to cat) are not refused.
- Worktrees live outside the protected control plane, so their writes are not mistaken for it.
- Destructive operations are enforced here, never left to a prompt.
- Decisions are logged to .loop/hook.log (tests point DEVTEAM_HOOK_LOG elsewhere).
- On an internal error the guard denies: it fails closed.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parent.parent).resolve()

READ_ONLY = {"reviewer", "cross-reviewer", "tech-lead", "phase-reviewer", "triage", "explorer",
             "qa-functional", "security-prober", "ui-reviewer"}
# Where each role may write (relative to the repo root unless absolute). {wt} = worktree dir.
WRITES = {
    "implementer": ["{wt}/task-*/**"],
    "qa-designer": ["{wt}/task-*/**"],
    "spec-author": ["work/**"],
    "reviewer": [".loop/runs/**"],
    "cross-reviewer": [".loop/runs/**"],
    "qa-functional": [".loop/runs/**", "/tmp/**", "/private/tmp/**"],
    "security-prober": [".loop/runs/**", "/tmp/**", "/private/tmp/**"],
    "ui-reviewer": [".loop/runs/**", "/tmp/**", "/private/tmp/**"],
    "triage": [".loop/runs/**"],
    "tech-lead": [".loop/precheck/**"],
    "phase-reviewer": [".loop/runs/**"],
    "runner": [],
    "explorer": [],
}
SAFE_READ = {"cat", "head", "tail", "less", "grep", "egrep", "rg", "ls", "find", "wc", "sort", "uniq",
             "cut", "tr", "diff", "stat", "file", "tree", "jq", "awk", "sed", "echo", "printf", "pwd",
             "cd", "basename", "dirname", "realpath", "true", "test", "[", "column", "nl", "xargs",
             "python3", "python", "pytest", "uv", "npm", "npx", "node", "make", "curl", "sleep", "date",
             "env", "which", "git"}
GIT_READ = {"diff", "log", "show", "status", "blame", "grep", "ls-files", "rev-parse", "merge-base",
            "branch", "cat-file", "describe", "shortlog"}
GIT_WRITE = {"commit", "merge", "rebase", "reset", "checkout", "switch", "push", "pull", "fetch",
             "worktree", "tag", "cherry-pick", "revert", "am", "apply", "stash", "restore", "clean",
             "update-ref", "branch", "gc", "filter-branch", "config", "remote"}
DESTRUCTIVE = [  # (pattern, applies_to_lead) — lead gets "ask", agents get "deny"
    (r"\bgit\s+push\b[^\n]*(--force|\s-f\b|--mirror|--delete)", True),
    (r"\bgit\s+reset\s+--hard\b", True),
    (r"\bgit\s+clean\s+-[a-z]*f", True),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME|\.\.?)(\s|/?$|\*)", True),
    (r"\b(DROP|TRUNCATE)\s+(DATABASE|SCHEMA|TABLE|WAREHOUSE|ROLE|USER)\b", True),
    (r"\bDELETE\s+FROM\b(?![^;]*\bWHERE\b)", True),
    (r"(curl|wget)[^|\n]*\|\s*(ba|z)?sh\b", True),
    (r"--no-verify|--dangerously", False),
    (r"\bchmod\s+-R\s+777\b", True),
]
RUNNER_CMD = re.compile(
    r"^\s*(cd\s+\S+\s*&&\s*)?python3\s+harness/referee\.py\s+(--strict\s+)?"
    r"(doctor|recover|plan|prepare|freeze|check|integrate|block|gate|status|ledger|reap|telemetry|wait)"
    r"(\s+[A-Za-z0-9_.,:-]+)*\s*$")


def cfg() -> dict:
    try:
        return tomllib.loads((ROOT / "harness" / "project.toml").read_text())
    except FileNotFoundError:
        return {}


def match(path: str, pat: str) -> bool:
    if fnmatch.fnmatchcase(path, pat):
        return True
    base = pat[:-3] if pat.endswith("/**") else pat.rstrip("/")
    return path == base or path.startswith(base + "/")


def norm(p: str, cwd: str) -> str:
    """Absolute or cwd-relative path -> repo-relative posix path (or absolute if outside the repo)."""
    q = Path(p).expanduser()
    q = (Path(cwd) / q) if not q.is_absolute() else q
    q = Path(os.path.normpath(q))
    try:
        return q.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        try:
            return q.relative_to(ROOT).as_posix()
        except ValueError:
            return q.as_posix()


def task_of(path: str, wtd: str):
    m = re.match(rf"^{re.escape(wtd)}/task-([^/]+)/(.+)$", path)
    return (m.group(1), m.group(2)) if m else (None, None)


def task_meta(tid: str) -> dict:
    for p in (ROOT / "work" / "tasks").glob(f"{tid}-*.md"):
        t = p.read_text()
        end = t.find("\n+++", 4)
        if t.startswith("+++\n") and end > 0:
            return tomllib.loads(t[4:end])
    return {}


def check_write(role: str, path: str) -> str | None:
    c = cfg()
    wtd = c.get("project", {}).get("worktree_dir", ".worktrees")
    protected = c.get("protected", {}).get("paths", [".claude/**", "harness/**", ".git/**", ".loop/state/**"])
    if role == "lead":
        return None  # the lead's control-plane edits go through permissions.ask in settings.json
    allowed = [g.replace("{wt}", wtd) for g in WRITES.get(role, [])]
    if not any(match(path, g) for g in allowed):
        return f"role '{role}' may not write {path}; allowed: {', '.join(allowed) or 'nothing'}"
    tid, inner = task_of(path, wtd)
    if tid:
        if any(match(inner, g) for g in protected):
            return f"{inner} is control plane (protected) inside the task worktree"
        meta = task_meta(tid)
        tests, files = meta.get("tests", []), meta.get("files", [])
        st = {}
        try:
            st = json.loads((ROOT / ".loop" / "state" / f"{tid}.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if role == "qa-designer":
            if not any(match(inner, g) for g in tests):
                return f"QA writes only the task's declared tests ({', '.join(tests)}); {inner} is not one"
            if st.get("status") not in (None, "prepared"):
                return f"task {tid} tests are already frozen"
        if role == "implementer":
            if inner in st.get("frozen", {}) or (st.get("status") == "frozen" and any(match(inner, g) for g in tests)):
                return f"{inner} is a frozen test (the oracle); report problems with it in unsure_about instead"
            if files and not any(match(inner, g) for g in files + tests):
                return (f"{inner} is outside task {tid}'s files ({', '.join(files)}); if it truly must change, "
                        f"stop and say so in unsure_about")
    elif any(match(path, g) for g in protected):
        return f"{path} is control plane (protected)"
    return None


def segments(cmd: str):
    """Split a shell command into simple commands; returns (list of token lists, redirect targets)."""
    lex = shlex.shlex(cmd.replace("\\\n", " "), posix=True, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    lex.commenters = ""
    toks = list(lex)
    segs, cur, redirects, i = [], [], [], 0
    while i < len(toks):
        t = toks[i]
        if t in (";", "&&", "||", "|", "&", "(", ")", ";;", "|&"):
            if cur:
                segs.append(cur)
            cur = []
        elif t in (">", ">>", ">|", "&>", "&>>"):
            if i + 1 < len(toks):
                redirects.append(toks[i + 1])
                i += 1
        elif t in ("<", "<<", "<<<", "<<-"):
            i += 1  # input redirections and heredoc tags are harmless
        else:
            cur.append(t)
        i += 1
    if cur:
        segs.append(cur)
    return segs, redirects


def git_verb(seg: list):
    i = 1
    while i < len(seg) and seg[i].startswith("-"):
        i += 2 if seg[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    return seg[i] if i < len(seg) else None


def check_bash(role: str, cmd: str, cwd: str) -> tuple[str, str] | None:
    """Returns (decision, reason) or None."""
    for pat, lead_too in DESTRUCTIVE:
        if re.search(pat, cmd, re.I):
            if role == "lead":
                return ("ask", f"destructive command needs your confirmation ({pat})") if lead_too else None
            return ("deny", "destructive command is not allowed for agents; report it to the lead instead")
    if role == "lead":
        return None
    if role == "runner":
        return None if RUNNER_CMD.match(cmd.strip()) else (
            "deny", "the runner may only run `python3 harness/referee.py <verb> [args]`")
    # \n inside a heredoc body is data; only split commands on the first line of each heredoc
    body_free = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\1\b", "<< EOF", cmd, flags=re.S)
    try:
        segs, redirects = segments(body_free.replace("\n", " ; "))
    except ValueError as e:
        return ("deny", f"could not parse the command ({e}); split it into simpler commands")
    for seg in segs:
        exe = os.path.basename(seg[0]) if seg else ""
        if exe == "git":
            v = git_verb(seg)
            if v in GIT_WRITE and not (v == "branch" and len(seg) <= 3 and "-D" not in seg and "-d" not in seg):
                return ("deny", f"git {v} is the referee's job; agents never write git")
        if role in READ_ONLY:
            if exe not in SAFE_READ:
                return ("deny", f"read-only role '{role}' may not run '{exe}'")
            if exe == "sed" and any(x.startswith("-i") or x == "--in-place" for x in seg):
                return ("deny", "sed -i edits files; read-only roles may not")
            if exe == "find" and any(x in ("-delete", "-exec", "-execdir", "-ok") for x in seg):
                return ("deny", "find -delete/-exec is not read-only")
    for target in redirects:
        if target.startswith("&") or target in ("/dev/null", "/dev/stderr", "/dev/stdout"):
            continue
        why = check_write(role, norm(target, cwd))
        if why:
            return ("deny", f"shell write: {why}")
    return None


def decide(role: str, ev: dict):
    tool, inp, cwd = ev.get("tool_name", ""), ev.get("tool_input") or {}, ev.get("cwd") or str(ROOT)
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        why = check_write(role, norm(p, cwd))
        return ("deny", why) if why else None
    if tool == "Bash":
        return check_bash(role, inp.get("command", ""), cwd)
    return None


def log(role: str, ev: dict, res) -> None:
    p = Path(os.environ.get("DEVTEAM_HOOK_LOG") or ROOT / ".loop" / "hook.log")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        inp = ev.get("tool_input") or {}
        what = inp.get("file_path") or inp.get("command") or ""
        with open(p, "a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "role": role, "tool": ev.get("tool_name"),
                                "decision": res[0] if res else "pass", "what": str(what)[:160],
                                "why": res[1][:200] if res else ""}) + "\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True)
    role = ap.parse_args().role
    try:
        ev = json.loads(sys.stdin.read() or "{}")
        res = decide(role, ev)
    except Exception as e:  # fail closed
        ev, res = {}, ("deny", f"guard error: {e!r}"[:300])
    log(role, ev, res)
    if res:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": res[0],
                                                 "permissionDecisionReason": res[1]}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
