import ast
import json
from pathlib import Path

src = Path("app.py").read_text(encoding="utf-8")
tree = ast.parse(src)

ui_inputs = set()
ui_outputs = set()
render_fns = set()
server_inputs = set()

for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr.startswith("input_") and node.args and isinstance(node.args[0], ast.Constant):
            ui_inputs.add(str(node.args[0].value))
        elif node.func.attr.startswith("output_") and node.args and isinstance(node.args[0], ast.Constant):
            ui_outputs.add(str(node.args[0].value))
        elif isinstance(node.func.value, ast.Name) and node.func.value.id == "input":
            server_inputs.add(node.func.attr)
    elif isinstance(node, ast.FunctionDef) and node. name != "server":
        render_fns.add(node.name)

input_aligned = bool(ui_inputs) and ui_inputs == server_inputs
output_aligned = bool(ui_outputs) and ui_outputs == render_fns

checks = [
    {"name": "input_ids_match", "passed": input_aligned},
    {"name": "output_ids_match", "passed": output_aligned},
]
score = sum(c["passed"] for c in checks) / len(checks)
print(json.dumps({"score": score, "success": all(c["passed"] for c in checks), "checks": checks}))
