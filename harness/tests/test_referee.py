"""End-to-end tests of the referee in a throwaway git repo: plan, prepare, freeze, check,
integrate, revert-on-red, blocking, jobs. Run: python3 -m pytest -q harness/tests"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent.parent
CONFIG = """
[project]
name = "t"
main_branch = "main"
test_cmd = "{py} -m pytest -q -p no:cacheprovider {{tests}}"
[budgets]
bash_wait_s = 60
[[gates]]
id = "unit"
cmd = "{py} -m pytest -q -p no:cacheprovider"
timeout = 120
smoke = true
[lanes]
light = ["docs/**"]
"""


def task_md(tid, phase="01", files=(), tests=(), deps=(), title="t"):
    q = lambda xs: "[" + ", ".join(f'"{x}"' for x in xs) + "]"
    return (f'+++\nid = "{tid}"\nphase = "{phase}"\ntitle = "{title}"\ndepends_on = {q(deps)}\n'
            f"files = {q(files)}\ntests = {q(tests)}\n+++\n\n## Goal\n{title}\n")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "harness").mkdir(parents=True)
    for f in ("referee.py", "guard.py", "tokens.py"):
        shutil.copy(HARNESS / f, r / "harness" / f)
    (r / "harness" / "project.toml").write_text(CONFIG.format(py=sys.executable))
    (r / "work" / "tasks").mkdir(parents=True)
    (r / "src").mkdir()
    (r / "src" / "__init__.py").write_text("")
    (r / "tests").mkdir()
    (r / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    (r / "conftest.py").write_text("")
    (r / ".gitignore").write_text(".loop/\n.worktrees/\n__pycache__/\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    g = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True, env=env)
    g("init", "-q", "-b", "main")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    os.environ.update({k: env[k] for k in env if k.startswith("GIT_")})
    return r


def ref(repo, *args, inline=True):
    cmd = [sys.executable, str(repo / "harness" / "referee.py"), *(["--inline"] if inline else []), *args]
    out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    line = out.stdout.strip().splitlines()[-1]
    assert len(line) <= 950, line
    return json.loads(line)


def add_task(repo, tid, **kw):
    (repo / "work" / "tasks" / f"{tid}-x.md").write_text(task_md(tid, **kw))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"task {tid}"], cwd=repo, check=True)


ADD_TEST = "from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def build_to_frozen(repo, tid="0001"):
    add_task(repo, tid, files=["src/calc.py"], tests=[f"tests/acceptance/test_{tid}.py"])
    assert ref(repo, "doctor")["passed"]
    assert ref(repo, "prepare", tid)["passed"]
    w = repo / ".worktrees" / f"task-{tid}"
    (w / "tests" / "acceptance").mkdir(parents=True)
    (w / "tests" / "acceptance" / f"test_{tid}.py").write_text(ADD_TEST)
    fz = ref(repo, "freeze", tid)
    assert fz["passed"], fz
    return w


def test_happy_path_merges_and_reaps(repo):
    w = build_to_frozen(repo)
    (w / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    ck = ref(repo, "check", "0001")
    assert ck["passed"], ck
    ig = ref(repo, "integrate", "0001")
    assert ig["passed"], ig
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout
    assert "merge(0001)" in log and (repo / "src" / "calc.py").exists()
    assert (repo / "work" / "evidence" / "0001.json").exists()
    assert ref(repo, "plan", "01")["merged"] == 1
    rp = ref(repo, "reap")
    assert rp["worktrees"] == 1 and rp["branches"] == 1 and not w.exists()
    assert ref(repo, "ledger")["passed"] and "| 0001 |" in (repo / "work" / "ledger.md").read_text()


def test_freeze_requires_red(repo):
    add_task(repo, "0002", files=["src/calc.py"], tests=["tests/acceptance/test_0002.py"])
    ref(repo, "prepare", "0002")
    w = repo / ".worktrees" / "task-0002"
    (w / "tests" / "acceptance").mkdir(parents=True)
    (w / "tests" / "acceptance" / "test_0002.py").write_text("def test_x():\n    assert True\n")
    assert ref(repo, "freeze", "0002")["code"] == "TESTS_NOT_RED"


def test_freeze_rejects_qa_writing_code(repo):
    add_task(repo, "0003", files=["src/calc.py"], tests=["tests/acceptance/test_0003.py"])
    ref(repo, "prepare", "0003")
    w = repo / ".worktrees" / "task-0003"
    (w / "tests" / "acceptance").mkdir(parents=True)
    (w / "tests" / "acceptance" / "test_0003.py").write_text(ADD_TEST)
    (w / "src" / "calc.py").write_text("x = 1\n")
    assert ref(repo, "freeze", "0003")["code"] == "SCOPE_VIOLATION"


def test_check_restores_frozen_tests_and_flags_scope(repo):
    w = build_to_frozen(repo)
    t = w / "tests" / "acceptance" / "test_0001.py"
    t.write_text("def test_add():\n    assert True\n")  # an implementer "fixing" the oracle
    ck = ref(repo, "check", "0001")
    assert ck["code"] == "FROZEN_MODIFIED" and t.read_text() == ADD_TEST
    (w / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (w / "src" / "other.py").write_text("y = 2\n")
    ck = ref(repo, "check", "0001")
    assert ck["code"] == "SCOPE_VIOLATION" and "src/other.py" in ck["out_of_scope"]


def test_gates_red_then_green(repo):
    w = build_to_frozen(repo)
    (w / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    ck = ref(repo, "check", "0001")
    assert ck["code"] == "GATES_RED" and ck["failing"] == ["task-tests"]
    detail = json.loads((repo / ck["detail"]).read_text())
    assert "assert" in detail["gates"][0]["tail"]
    assert ref(repo, "integrate", "0001")["code"] == "NOT_CHECKED"


def test_smoke_red_after_merge_reverts(repo):
    w = build_to_frozen(repo)
    (w / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    assert ref(repo, "check", "0001")["passed"]
    cfgp = repo / "harness" / "project.toml"  # smoke-only gate that fails on main
    cfgp.write_text(cfgp.read_text() + '\n[[gates]]\nid = "smoke-only"\ncmd = "false"\ncheck = false\nintegrate = false\nphase = false\nsmoke = true\n')
    subprocess.run(["git", "commit", "-qam", "cfg"], cwd=repo, check=True)
    ig = ref(repo, "integrate", "0001")
    assert ig["code"] == "MAIN_RED_AFTER_MERGE", ig
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=repo, capture_output=True, text=True).stdout
    assert log.split(" ", 1)[1].startswith("Revert")
    st = ref(repo, "status")["phases"]["01"]
    assert st["blocked"] == ["0001:MAIN_RED_AFTER_MERGE"]


def test_plan_dependencies_overlap_and_lanes(repo):
    add_task(repo, "0010", files=["src/a.py"], tests=["tests/acceptance/test_0010.py"])
    add_task(repo, "0011", files=["src/a.py"], tests=["tests/acceptance/test_0011.py"])  # overlaps 0010
    add_task(repo, "0012", files=["src/b.py"], tests=["tests/acceptance/test_0012.py"], deps=["0010"])
    add_task(repo, "0013", files=["docs/guide.md"])
    p = ref(repo, "plan", "01")
    assert [t["id"] for t in p["ready"]] == ["0010", "0013"]
    assert set(p["waiting"]) == {"0011", "0012"}
    assert {t["id"]: t["lane"] for t in p["ready"]}["0013"] == "light"
    assert p["precheck_needed"]
    p2 = ref(repo, "plan", "01", "--busy", "0010", "--exclude", "0013")
    assert p2["ready"] == []


def test_block_unblock_and_long_verb_job(repo):
    w = build_to_frozen(repo)
    (w / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    ck = ref(repo, "check", "0001", inline=False)  # detached job path
    assert ck["passed"], ck
    assert ref(repo, "block", "0001", "ENV")["passed"]
    assert ref(repo, "prepare", "0001")["code"] == "BLOCKED"
    assert ref(repo, "unblock", "0001")["status"] == "frozen"


def test_doctor_reports_failures(repo):
    cfgp = repo / "harness" / "project.toml"
    cfgp.write_text(cfgp.read_text() + '\n[[doctor]]\nname = "db"\ncmd = "echo no database >&2; false"\n')
    d = ref(repo, "doctor")
    assert not d["passed"] and any("db" in f for f in d["failures"])
