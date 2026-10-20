import ast
import json
from pathlib import Path

src = Path("app.py").read_text(encoding="utf-8")
tree = ast.parse(src)

server_fn = next(
    (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "server"), None
)
has_reactive_calc = False
compute_calls_in_server = 0
if server_fn:
    for node in ast.walk(server_fn):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Attribute)
                    and isinstance(dec.value, ast.Name)
                    and dec.value.id == "reactive"
                    and dec.attr in {"calc", "Calc"}
                ):
                    has_reactive_calc = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "compute_metrics"
        ):
            compute_calls_in_server += 1

checks = [
    {"name": "uses_reactive_calc", "passed": has_reactive_calc},
    {"name": "single_compute_metrics_call", "passed": compute_calls_in_server == 1},
]
score = sum(c["passed"] for c in checks) / len(checks)
print(json.dumps({"score": score, "success": all(c["passed"] for c in checks), "checks": checks}))
