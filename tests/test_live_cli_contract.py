"""Opt-in, credential-free checks of actual current binaries used by CI."""

import json
import os

import pytest

from skilldiff.config import ExperimentConfig
from skilldiff.runner import AgentRunner
from skilldiff.workspace import Workspace


@pytest.mark.skipif(not os.environ.get("SKILLDIFF_LIVE_CLI"), reason="opt-in installed CLI probe")
def test_installed_current_cli_contract_without_credentials(tmp_path, monkeypatch):
    harness = os.environ["SKILLDIFF_LIVE_CLI"]
    runner = AgentRunner()  # Resolve binaries before replacing HOME/PATH-derived state.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, rel in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"),
                     ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(key, str(home / rel))
    cfg = ExperimentConfig(name="cli-contract", skill=None, models=["unused"],
                           tasks_patterns=[], harness=harness)
    caps = runner.capabilities(tmp_path, cfg, tmp_path)
    assert caps["version"]
    assert caps["commands"]
    if harness == "codex":
        skill = tmp_path / "probe-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: probe-skill\ndescription: CLI probe\n---\n")
        cfg.skill = skill
        for installed in (False, True):
            ws = Workspace(tmp_path / str(installed) / "workspace", installed, skill,
                           harness=harness)
            ws.setup()
            report = runner.preflight(ws.root, cfg, ["probe-skill"] if installed else [])
            assert any(s["name"] == "probe-skill" for s in report["skills"]) is installed
        schema_dir = tmp_path / "schema"
        result = runner._exec([runner.codex_bin, "app-server", "generate-json-schema",
                               "--out", str(schema_dir)], tmp_path, os.environ.copy(), 30)
        assert result.exit_code == 0, result.stderr
        definitions = [json.loads(p.read_text()) for p in schema_dir.rglob("*.json")]
        assert any("ephemeral" in str(d) and "ThreadStartParams" in str(d) for d in definitions)
