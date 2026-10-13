"""External CLI doubles; protocol and capability contracts have dedicated tests."""
import sys

import pytest

from skilldiff.runner import AgentRunner


@pytest.fixture
def capable_cli(monkeypatch):
    """Keep grader/discovery tests independent of the credential-free version probe."""
    monkeypatch.setattr(AgentRunner, "capabilities", lambda self, cwd, cfg, scratch: {
        "version": "test-cli 2.0.0", "binary": self.binary_for(cfg.harness, cfg),
        "isolation": cfg.isolation, "commands": {}})


@pytest.fixture
def conversation_cli(tmp_path):
    agent = tmp_path / "agent"
    agent.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys, time
args = sys.argv[1:]
def emit(value):
    print(json.dumps(value), flush=True)
def record():
    directory=pathlib.Path(os.environ['TMPDIR'])
    marker=directory/'previous-turn'
    evidence={'temp':str(directory),'previous':marker.exists(),
              'env':[os.environ.get(k) for k in ('TMPDIR','TMP','TEMP')]}
    with pathlib.Path('evidence.jsonl').open('a') as stream:
        stream.write(json.dumps(evidence)+'\\n')
    marker.write_text('session')
if "--help" in args:
    print("--auto --format --standalone --session --ephemeral --json --input-format "
          "--output-format --verbose --no-session-persistence --setting-sources --stdio")
    sys.exit()
if "--version" in args:
    print("test-cli 2.0.0")
    sys.exit()
if "app-server" in args:
    thread = "thread-" + str(os.getpid())
    remembered = None
    for line in sys.stdin:
        req = json.loads(line)
        method = req["method"]
        if method == "initialize":
            emit({"id": req["id"], "result": {}})
        elif method == "skills/list":
            emit({"id": req["id"], "result": {"data": []}})
        elif method == "thread/start":
            assert req["params"]["ephemeral"] is True
            emit({"id": req["id"], "result": {"thread": {"id": thread}}})
        elif method == "turn/start":
            assert req["params"]["threadId"] == thread
            record()
            text = req["params"]["input"][0]["text"]
            if text.startswith("remember "):
                remembered = text[9:]
            if text == "hang":
                time.sleep(10)
            emit({"id": req["id"], "result": {"turn": {"id": str(os.getpid()),
                  "status": "inProgress"}}})
            emit({"method": "item/completed", "params": {"threadId": thread,
                  "item": {"type": "agentMessage", "text": remembered}}})
            emit({"method": "thread/tokenUsage/updated", "params": {"threadId": thread,
                  "tokenUsage": {"total": {"inputTokens": 200, "cachedInputTokens": 50,
                                 "outputTokens": 30, "totalTokens": 230}}}})
            emit({"method": "turn/completed", "params": {"threadId": thread,
                  "turn": {"id": str(os.getpid()), "status":
                           "failed" if text == "fail" else "completed",
                           "error": {"message": "turn failed"} if text == "fail" else None}}})
    sys.exit()
if "run" in args:
    session = args[args.index("--session") + 1]
    path = pathlib.Path("context-" + session)
    record()
    text = args[-1]
    if text.startswith("remember "):
        path.write_text(text[9:])
    if text == "hang":
        time.sleep(10)
    if text == "fail":
        emit({"type": "error", "error": {"message": "turn failed"}})
    else:
        emit({"type": "text", "sessionID": session,
              "part": {"text": path.read_text() if path.exists() else "LOST"}})
        emit({"type": "step_finish", "sessionID": session,
              "part": {"id": "step", "reason": "stop",
                       "tokens": {"input": 100, "output": 15, "cache": {"read": 0,
                                  "write": 0}}, "cost": 0}})
    sys.exit()
is_agy = "--add-dir" in args
remembered = None
turn = 0
for line in sys.stdin:
    msg = json.loads(line)
    record()
    text = msg["message"]["content"]
    turn += 1
    if text.startswith("remember "):
        remembered = text[9:]
    if text == "hang":
        time.sleep(10)
    if is_agy:
        emit({"event": "result", "result": {"status": "ERROR" if text == "fail" else
              "SUCCESS", "error": "turn failed" if text == "fail" else None,
              "conversation_id": "agy-" + str(os.getpid()), "response": remembered,
              "num_turns": turn, "usage": {"input_tokens": 100 * turn,
              "output_tokens": 15 * turn, "cache_read_tokens": 20 * turn,
              "total_tokens": 115 * turn}}})
    else:
        emit({"type": "result", "subtype": "error_during_execution" if text == "fail"
              else "success", "session_id": "claude-" + str(os.getpid()),
              "result": remembered, "num_turns": 1, "total_cost_usd": 0.01 * turn,
              "usage": {"input_tokens": 100, "output_tokens": 15,
              "cache_read_input_tokens": 20, "cache_creation_input_tokens": 0}})
''')
    agent.chmod(0o755)
    return str(agent)
