"""Drive the real build-phase workflow script with stubbed agents (no model calls) against a
throwaway repo. Skipped when node is not installed. Run: python3 -m pytest -q harness/tests"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_referee import add_task, ref, repo  # noqa: F401  (fixture)

HERE = Path(__file__).resolve().parent
WORKFLOW = HERE.parent.parent / ".claude" / "workflows" / "build-phase.js"
ADD = "def add(a, b):\n    return a + b\n"
MUL = "def mul(a, b):\n    return a * b\n"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def simulate(repo, scenario, tmp_path):
    scenario = {"python": sys.executable, "phase": "01", "args": {"phase": "01", "request": "run phase 01"}, **scenario}
    sp = tmp_path / "scenario.json"
    sp.write_text(json.dumps(scenario))
    out = subprocess.run(["node", str(HERE / "sim_workflow.mjs"), str(WORKFLOW), str(repo), str(sp)],
                         capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_phase_merges_with_retry_review_round_and_escalation(repo, tmp_path):
    add_task(repo, "0001", files=["src/calc.py"], tests=["tests/acceptance/test_0001.py"])
    add_task(repo, "0002", files=["src/mul.py"], tests=["tests/acceptance/test_0002.py"], deps=["0001"])
    sc = {"tasks": {
        "0001": {"tests": {"tests/acceptance/test_0001.py": "from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"},
                 "impl": [{"src/calc.py": "def add(a, b):\n    return a - b\n"}, {"src/calc.py": ADD}]},
        "0002": {"tests": {"tests/acceptance/test_0002.py": "from src.mul import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"},
                 "impl": [{"src/mul.py": MUL}], "review_blocking_rounds": 1,
                 "cross_candidates": [{"file": "src/mul.py", "line": 1, "scenario": "stub candidate"}]}}}
    r = simulate(repo, sc, tmp_path)
    res = r["result"]
    assert res["merged"] == ["0001", "0002"], r["logs"]
    assert res["gate"]["passed"] and res["follow_ups"] == ["stub follow-up"]
    roles = [c["role"] for c in r["calls"]]
    assert roles.count("tech-lead") == 1 and "triage" in roles
    impl_0001 = [c for c in r["calls"] if c["role"] == "implementer" and c["id"] == "0001"]
    assert len(impl_0001) == 2 and impl_0001[0]["model"] is None  # ds-flash first, from frontmatter
    # the free cross-review runs before every deciding review, and its candidates reach the reviewer
    seq = [(c["role"], c["id"]) for c in r["calls"] if c["role"] in ("cross-reviewer", "reviewer")]
    assert seq[:2] == [("cross-reviewer", "0001"), ("reviewer", "0001")], seq
    rev = {c["id"]: c for c in r["calls"] if c["role"] == "reviewer"}
    assert rev["0002"]["cross"] and not rev["0001"]["cross"]
    fixes = [c for c in r["calls"] if c["role"] == "implementer" and c["id"] == "0002"]
    assert len(fixes) == 2  # build + one review fix
    assert (repo / "work" / "ledger.md").exists() and (repo / "work" / "evidence" / "0002.json").exists()
    # second launch: everything merged, plan check cached, no build agents spent
    r2 = simulate(repo, sc, tmp_path)
    assert r2["result"]["merged"] == [] and not [c for c in r2["calls"] if c["role"] not in ("runner",)]


def test_env_failure_blocks_without_spending_attempts(repo, tmp_path):
    add_task(repo, "0003", files=["src/calc.py"], tests=["tests/acceptance/test_0003.py"])
    sc = {"tasks": {"0003": {
        "tests": {"tests/acceptance/test_0003.py": "from src.calc import add\n\ndef test_add():\n    assert add(1, 1) == 2\n"},
        "impl": [{"src/calc.py": "def add(a, b):\n    return 0\n"}], "triage": "env"}}}
    r = simulate(repo, sc, tmp_path)
    assert r["result"]["blocked"] == [{"id": "0003", "code": "ENV"}]
    assert len([c for c in r["calls"] if c["role"] == "implementer"]) == 1
    assert ref(repo, "status")["phases"]["01"]["blocked"] == ["0003:ENV"]


def test_last_attempt_escalates(repo, tmp_path):
    add_task(repo, "0004", files=["src/calc.py"], tests=["tests/acceptance/test_0004.py"])
    bad = {"src/calc.py": "def add(a, b):\n    return 0\n"}
    sc = {"tasks": {"0004": {
        "tests": {"tests/acceptance/test_0004.py": "from src.calc import add\n\ndef test_add():\n    assert add(1, 1) == 2\n"},
        "impl": [bad, bad, bad]}}}
    r = simulate(repo, sc, tmp_path)
    impl = [c for c in r["calls"] if c["role"] == "implementer"]
    assert [c["model"] for c in impl] == [None, None, "role-escalate"]
    assert r["result"]["blocked"] == [{"id": "0004", "code": "ATTEMPTS_EXHAUSTED"}]


def test_unusable_support_output_is_rescued_once(repo, tmp_path):
    # the free runner garbles a check step and free triage returns nothing: each is retried once on
    # role-rescue, and the task still merges
    add_task(repo, "0006", files=["src/calc.py"], tests=["tests/acceptance/test_0006.py"])
    sc = {"runner_garbles": "check 0006", "tasks": {"0006": {
        "tests": {"tests/acceptance/test_0006.py": "from src.calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"},
        "impl": [{"src/calc.py": "def add(a, b):\n    return 0\n"}, {"src/calc.py": ADD}], "triage_fails_once": True}}}
    r = simulate(repo, sc, tmp_path)
    assert r["result"]["merged"] == ["0006"], r["logs"]
    rescued = [(c["role"], c["label"]) for c in r["calls"] if c["model"] == "role-rescue"]
    assert ("runner", "referee check 0006 (rescue)") in rescued, rescued
    assert any(role == "triage" for role, _ in rescued), rescued
    assert r["result"]["rescues"] == len(rescued)
    assert not [c for c in r["calls"] if c["model"] == "role-rescue" and c["role"] in ("implementer", "reviewer")]


def test_judgement_failure_blocks_instead_of_downgrading(repo, tmp_path):
    add_task(repo, "0007", files=["src/calc.py"], tests=["tests/acceptance/test_0007.py"])
    sc = {"tasks": {"0007": {
        "tests": {"tests/acceptance/test_0007.py": "from src.calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"},
        "impl": [{"src/calc.py": ADD}], "reviewer_fails": True}}}
    r = simulate(repo, sc, tmp_path)
    assert r["result"]["blocked"] == [{"id": "0007", "code": "REVIEWER_FAILED"}]
    assert not [c for c in r["calls"] if c["model"] == "role-rescue"]


def test_precheck_blocking_stops_before_build(repo, tmp_path):
    add_task(repo, "0005", files=["src/calc.py"], tests=["tests/acceptance/test_0005.py"])
    sc = {"precheck_blocking": [{"task": "0005", "issue": "names a missing module", "fix": "add a dependency"}],
          "tasks": {"0005": {"tests": {}, "impl": [{}]}}}
    r = simulate(repo, sc, tmp_path)
    assert r["result"]["stopped"] == "PRECHECK_BLOCKING"
    assert not [c for c in r["calls"] if c["role"] in ("qa-designer", "implementer")]
