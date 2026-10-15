import ast
import json
from pathlib import Path

src = Path("app.py").read_text(encoding="utf-8")
tree = ast.parse(src)

module_mutable = any(
    isinstance(node, ast.Assign)
    and isinstance(node.value, (ast.List, ast.Dict, ast.Set))
    for node in tree.body
)
server_fn = next(
    (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "server"), None
)
has_session_reactive = False
if server_fn:
    has_session_reactive = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "reactive"
        and n.func.attr in {"value", "Value"}
        for n in ast.walk(server_fn)
    )

checks = [
    {"name": "no_module_mutable_state", "passed": not module_mutable},
    {"name": "uses_per_session_reactive_value", "passed": has_session_reactive},
]
score = sum(c["passed"] for c in checks) / len(checks)
print(json.dumps({"score": score, "success": all(c["passed"] for c in checks), "checks": checks}))
