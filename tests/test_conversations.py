"""Exercise native conversation transports through real, credential-free processes."""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from skilldiff.config import AntigravityConfig, ClaudeConfig, CodexConfig, OpenCodeConfig
from skilldiff.runner import AgentRunner

CONFIGS = [ClaudeConfig, CodexConfig, AntigravityConfig, OpenCodeConfig]


@pytest.mark.parametrize("config_class", CONFIGS)
def test_followup_keeps_conversation_context(tmp_path, conversation_cli, config_class):
    cfg = config_class(bin_path=conversation_cli)
    result = AgentRunner().run(["remember unique-secret", "recall"], tmp_path, "m", cfg,
                               timeout=5)
    assert result.status == "ok", result.error
    assert result.response == "unique-secret"
    assert result.session_id
    assert result.input_tokens in (150, 160, 200)
    assert result.output_tokens == 30


@pytest.mark.parametrize("config_class", CONFIGS)
def test_failed_turn_stops_before_next_prompt(tmp_path, conversation_cli, config_class):
    result = AgentRunner().run(["remember kept", "fail", "remember forbidden"], tmp_path,
                               "m", config_class(bin_path=conversation_cli), timeout=5)
    assert result.status == "error"
    assert result.failure_kind == "harness_error"
    assert "forbidden" not in result.transcript
    assert "kept" in result.transcript
    assert result.input_tokens is not None


@pytest.mark.parametrize("config_class", CONFIGS)
def test_conversation_deadline_keeps_completed_turn(tmp_path, conversation_cli, config_class):
    result = AgentRunner().run(["remember kept", "hang"], tmp_path, "m",
                               config_class(bin_path=conversation_cli), timeout=0.5)
    assert result.status == "timeout"
    assert "kept" in result.transcript
    assert result.input_tokens is not None
    assert result.duration < 5


@pytest.mark.parametrize("config_class", CONFIGS)
def test_parallel_conversations_have_distinct_sessions(tmp_path, conversation_cli, config_class):
    runner = AgentRunner()
    def run(word):
        ws = tmp_path / word
        ws.mkdir()
        return runner.run(["remember " + word, "recall"], ws, "m",
                          config_class(bin_path=conversation_cli), timeout=5)
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(run, ["apple", "pear"])
    assert (a.response, b.response) == ("apple", "pear")
    assert a.session_id != b.session_id


@pytest.mark.parametrize("config_class", [ClaudeConfig, AntigravityConfig])
def test_malformed_terminal_stops_before_followup(tmp_path, config_class):
    cli = tmp_path / "malformed-agent"
    cli.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, sys
for index, line in enumerate(sys.stdin):
    with pathlib.Path('prompts').open('a') as log:
        log.write(line)
    event = {"event":"result","result":{"status":"SUCCESS","usage":[1]}} \\
            if "--add-dir" in sys.argv else {"type":"result","usage":[1]}
    print(json.dumps(event), flush=True)
''')
    cli.chmod(0o755)
    result = AgentRunner().run(["one", "two"], tmp_path, "m", config_class(bin_path=str(cli)),
                               timeout=3)
    assert result.failure_kind == "harness_error"
    assert len((tmp_path / "prompts").read_text().splitlines()) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group cleanup")
@pytest.mark.parametrize("config_class", [ClaudeConfig, AntigravityConfig])
def test_streaming_completion_cleans_pipe_holding_children_immediately(tmp_path, config_class):
    cli = tmp_path / "child-agent"
    cli.write_text(f"#!{sys.executable}\n" + '''
import json, subprocess, sys
is_agy = "--add-dir" in sys.argv
turn = 0
for line in sys.stdin:
    turn += 1
    if turn == 2:
        subprocess.Popen([sys.executable, "-c", "import time,pathlib;time.sleep(.4);"
                          "pathlib.Path('escaped-write').write_text('bad')"])
    event = {"event":"result","result":{"status":"SUCCESS","response":"done"}} if is_agy \
            else {"type":"result","result":"done"}
    print(json.dumps(event), flush=True)
''')
    cli.chmod(0o755)
    result = AgentRunner().run(["one", "two"], tmp_path, "m", config_class(bin_path=str(cli)),
                               timeout=3)
    assert result.status == "ok", result.error
    time.sleep(.5)
    assert not (tmp_path / "escaped-write").exists()
