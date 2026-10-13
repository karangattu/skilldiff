import json

import pytest

from skilldiff.config import ExperimentConfig
from skilldiff.runner import AgentRunner, ExecResult


def test_capability_probe_uses_selected_container_and_records_versions(tmp_path):
    class Runner(AgentRunner):
        def _exec(self, cmd, cwd, env, timeout, **kwargs):
            assert kwargs["isolation"] == "docker"
            assert kwargs["container_image"] == "sha256:test"
            assert cmd[0] == "opencode"
            if cmd[-1] == "--version":
                return ExecResult("v2.0.23\n", "", 0, 0)
            assert cmd[1:] == ["run", "--help"]
            return ExecResult("--format json --standalone --session --auto", "", 0, 0)
    cfg = ExperimentConfig(name="test", skill=None, models=["m"], tasks_patterns=[],
                           harness="opencode", isolation="docker", container_image="sha256:test")
    caps = Runner(opencode_bin="/host/opencode").capabilities(tmp_path, cfg, tmp_path)
    assert caps["version"] == "v2.0.23"
    assert caps["commands"]["run"]["flags"] == ["--auto", "--format", "--session", "--standalone"]
    assert json.loads(json.dumps(caps)) == caps


@pytest.mark.parametrize("output", ["--format json", "not help"])
def test_missing_required_capabilities_fail_before_model_calls(tmp_path, output):
    class Runner(AgentRunner):
        def _exec(self, cmd, *args, **kwargs):
            assert cmd[-1] in {"--version", "--help"}
            return ExecResult("v2.0.23" if cmd[-1] == "--version" else output, "", 0, 0)
    cfg = ExperimentConfig(name="test", skill=None, models=["m"], tasks_patterns=[],
                           harness="opencode")
    with pytest.raises(RuntimeError, match="capabilit"):
        Runner().capabilities(tmp_path, cfg, tmp_path)
