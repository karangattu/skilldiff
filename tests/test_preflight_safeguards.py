"""Failures observed in a real paired Shiny repair evaluation."""

import sys

import pytest

from skilldiff.config import ExperimentConfig, GraderConfig
from skilldiff.grader import Grader, validate_grader_against_directories
from skilldiff.reporter import build_decision_context, build_markdown_report
from skilldiff.runner import AgentRunner


def test_validation_explains_missing_browser_instead_of_losing_feedback(tmp_path):
    grader = Grader(
        GraderConfig(
            command=f"{sys.executable} -c 'raise RuntimeError(\"Missing Chromium executable\")'"
        ),
        skill_name="s",
        model_name="m",
    )
    report = validate_grader_against_directories(grader, tmp_path)
    assert report["verdict"] == "grader-broken"
    assert "Missing Chromium executable" in report["untouched"]["feedback"]


def test_known_good_solution_must_obey_the_scope_given_to_agents(tmp_path):
    fixture, good = tmp_path / "fixture", tmp_path / "good"
    fixture.mkdir()
    good.mkdir()
    (fixture / "app.py").write_text("broken")
    (good / "app.py").write_text("fixed")
    (fixture / "warehouse.py").write_text("shared")
    (good / "warehouse.py").write_text("private")
    command = (
        f"{sys.executable} -c 'import pathlib,json; "
        'print(json.dumps({"score": float(pathlib.Path("app.py").read_text() == "fixed")}))\''
    )
    grader = Grader(
        GraderConfig(command=command),
        skill_name="s",
        model_name="m",
        forbidden_paths=["warehouse.py"],
    )
    report = validate_grader_against_directories(grader, fixture, good)
    assert report["verdict"] == "scope-conflict"
    assert "warehouse.py" in " ".join(report["checks"])


def cost_results(basis, repetitions=1):
    runs = {"control": [], "treatment": []}
    for i in range(3):
        for arm, tokens in [("control", 1_000_000), ("treatment", 500_000)]:
            runs[arm].append(
                {
                    "model": "m",
                    "task_id": str(i),
                    "repetition": 1,
                    "arm": arm,
                    "status": "ok",
                    "grade_status": "graded",
                    "score": 1,
                    "success": True,
                    "cost": None,
                    "input_tokens": tokens,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "output_tokens": 0,
                }
            )
    if repetitions > 1:
        for arm, records in runs.items():
            runs[arm] = [
                {**run, "repetition": rep,
                 "score": 0.8 if arm == "control" else 0.8 + 0.02 * (int(run["task_id"]) + rep),
                 "success": False}
                for rep in range(1, repetitions + 1) for run in records
            ]
    return {
        "name": "cost",
        "preset": "compression",
        "runs": runs,
        "cost_basis": basis,
        "thresholds": {"required_cost_reduction_pct": 10},
        "pricing": {
            "source": "https://example.com",
            "date": "2026-10-07",
            "rates": {"m": {"input": 2, "output": 10, "cache_read": 0.1, "cache_write": 2.5}},
        },
    }


def test_recorded_api_cost_is_used_in_the_decision_and_reports():
    results = cost_results("api-equivalent", repetitions=2)
    decision = build_decision_context(results)
    assert decision["paired"]["cost"]["n"] == 6
    assert decision["paired"]["cost"]["mean_diff"] == -1
    assert decision["recommendation"][1] == "SHIP"
    assert "API-equivalent" in build_markdown_report(results)
    assert "cost data missing" not in build_markdown_report(results)


def test_legacy_reports_keep_harness_cost_and_missing_usage_stays_unknown():
    results = cost_results("harness")
    assert build_decision_context(results)["paired"]["cost"]["n"] == 0
    results.pop("cost_basis")
    assert build_decision_context(results)["paired"]["cost"]["n"] == 0
    results["cost_basis"] = "api-equivalent"
    results["runs"]["treatment"][0]["input_tokens"] = None
    assert build_decision_context(results)["paired"]["cost"]["n"] == 2


def test_codex_discovery_detects_a_present_but_undiscoverable_skill(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = workspace / ".agents/skills/sample"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: sample\ndescription: Example\n---\n")
    binary = tmp_path / "codex"
    binary.write_text(f"""#!{sys.executable}
import json,sys
for line in sys.stdin:
    message=json.loads(line)
    if message.get('method')=='initialize':
        print(json.dumps({{'id':message['id'],'result':{{}}}}), flush=True)
    elif message.get('method')=='skills/list':
        reply={{'id':message['id'],'result':{{'data':[{{'skills':[], 'errors':[]}}]}}}}
        print(json.dumps(reply),flush=True)
""")
    binary.chmod(0o755)
    config = ExperimentConfig(
        name="t", skill=skill, models=["m"], tasks_patterns=[], harness="codex"
    )
    config.codex.bin_path = str(binary)
    with pytest.raises(
        RuntimeError, match="(?i)(discover|available).*sample|sample.*(discover|available)"
    ):
        AgentRunner().preflight(workspace, config, expected_skills=["sample"])


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS read isolation")
def test_native_sandbox_allows_skill_walk_and_blocks_sibling_content(tmp_path):
    workspace = tmp_path / "session/workspace"
    workspace.mkdir(parents=True)
    skill = workspace / ".agents/skills/sample"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("available")
    sibling = tmp_path / "sibling/secret"
    sibling.parent.mkdir()
    sibling.write_text("hidden")
    scratch = tmp_path / "session/scratch"
    scratch.mkdir()
    code = f"""import pathlib,os
p=pathlib.Path('.agents/skills')
assert list(p.rglob('SKILL.md'))[0].read_text() == 'available'
try: pathlib.Path({str(sibling)!r}).read_text()
except PermissionError: pass
else: raise AssertionError('sibling readable')
pathlib.Path(os.environ['TMPDIR'],'probe').write_text('ok')
print('verified')"""
    result = AgentRunner()._exec(
        [sys.executable, "-c", code],
        workspace,
        {},
        10,
        isolation="macos",
        temp_dir=scratch,
        read_paths=[sys.prefix, sys.base_prefix],
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "verified"


def test_cost_basis_is_explicit_and_validated_in_config(tmp_path):
    from skilldiff.config import load_experiment

    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Example")
    (tmp_path / "task.yaml").write_text("id: one\nprompt: Repair\n")
    config = tmp_path / "skilldiff.yaml"
    base = "name: t\nskill: ./skill\nmodels: [m]\ntasks: [./task.yaml]\n"
    config.write_text(base + "cost_basis: api-equivalent\n")
    with pytest.raises(ValueError, match="pricing"):
        load_experiment(config)
    config.write_text(base + "cost_basis: unexpected\n")
    with pytest.raises(ValueError, match="cost_basis"):
        load_experiment(config)
    config.write_text(base + "cost_basis: harness\n")
    assert load_experiment(config)[0].cost_basis == "harness"


def test_scope_instructions_are_identical_and_describe_ignore_precedence():
    from skilldiff.scope import scope_instructions

    text = scope_instructions(["app.py"], ["warehouse.py"], ["outputs/*"])
    assert "app.py" in text and "warehouse.py" in text and "outputs/*" in text
    assert "any depth" in text and "excluded" in text


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS read isolation")
@pytest.mark.parametrize("location", [".agents", "lib/site-packages/shiny/.agents"])
def test_native_runtime_root_cannot_expose_protected_skill(tmp_path, location):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    bundled = runtime / location / "skills/secret"
    bundled.mkdir(parents=True)
    secret = bundled / "SKILL.md"
    secret.write_text("hidden")
    (runtime / "module.py").write_text("allowed")
    code = f"""from pathlib import Path
assert Path({str(runtime / "module.py")!r}).read_text() == 'allowed'
try: Path({str(secret)!r}).read_text()
except PermissionError: pass
else: raise AssertionError('bundled skill readable')"""
    result = AgentRunner()._exec(
        [sys.executable, "-c", code],
        workspace,
        {},
        10,
        isolation="macos",
        temp_dir=scratch,
        read_paths=[sys.prefix, sys.base_prefix, str(runtime)],
        protected_paths=[],
    )
    assert result.exit_code == 0, result.stderr


def test_cost_decision_is_consistent_across_cli_and_reports():
    from skilldiff.cli import _format_results
    from skilldiff.reporter import build_html_report

    results = cost_results("api-equivalent", repetitions=2)
    results.update(models=["m"], tasks=["0", "1", "2"], tasks_count=3, runs_per_arm=2)
    for output in (
        _format_results(results),
        build_markdown_report(results),
        build_html_report(results),
    ):
        assert "Recommendation: SHIP" in output
        assert "API-equivalent" in output
        assert "cost data missing" not in output
        assert "Held-out data only" not in output
    assert all(run["cost"] is None for run in results["runs"]["control"])


def test_saved_aggregates_use_decision_cost_without_overwriting_arm_telemetry(tmp_path):
    from skilldiff.config import TaskConfig
    from skilldiff.experiment import ExperimentRunner

    source = cost_results("api-equivalent")
    cfg = ExperimentConfig(
        name="cost",
        skill=None,
        models=["m"],
        tasks_patterns=[],
        cost_basis="api-equivalent",
        pricing=source["pricing"],
    )
    tasks = [TaskConfig(id=str(i), prompt="Do work") for i in range(3)]
    runner = ExperimentRunner(cfg, tasks)
    result = runner._aggregate(
        tmp_path,
        "now",
        source["runs"]["control"],
        source["runs"]["treatment"],
        [],
        False,
        provenance_snapshot={"test": True},
    )
    assert result["overall"]["paired"]["cost"]["n"] == 3
    assert result["overall"]["paired"]["cost"]["mean_diff"] == -1
    assert result["runs"]["control"][0]["cost"] is None


def test_revision_comparison_uses_recorded_cost_basis_and_rejects_a_mismatch():
    import copy

    from skilldiff.compare import compare_results

    a = cost_results("api-equivalent")
    b = copy.deepcopy(a)
    for run in b["runs"]["treatment"]:
        run["input_tokens"] //= 2
    result = compare_results(a, b)
    assert result["efficiency"]["cost"]["a"] == 1
    assert result["efficiency"]["cost"]["b"] == 0.5
    b["cost_basis"] = "harness"
    with pytest.raises(ValueError, match="(?i)cost.*bas"):
        compare_results(a, b, strict=True)


def test_container_discovery_uses_image_binary_name_not_discovered_host_path(tmp_path):
    from skilldiff.runner import ExecResult

    class CaptureRunner(AgentRunner):
        def _exec(self, cmd, *args, **kwargs):
            assert cmd[0] == "codex"
            return ExecResult('{"id":2,"result":{"data":[]}}\n', "", 0, 0)

    cfg = ExperimentConfig(
        name="container",
        skill=None,
        models=["m"],
        tasks_patterns=[],
        harness="codex",
        isolation="docker",
    )
    CaptureRunner(codex_bin="/host/tools/codex").preflight(tmp_path, cfg, [])
