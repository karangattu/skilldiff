import ast
import json
from pathlib import Path

src = Path("app.py").read_text(encoding="utf-8")
tree = ast.parse(src)

has_module_ui = False
has_module_server = False
module_ui_functions = set()
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef):
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Attribute)
                and isinstance(dec.value, ast.Name)
                and dec.value.id == "module"
            ):
                if dec.attr == "ui":
                    has_module_ui = True
                    module_ui_functions.add(node.name)
                elif dec.attr == "server":
                    has_module_server = True

namespaces = {
    node.args[0].value
    for node in ast.walk(tree)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    and node.func.id in module_ui_functions and node.args
    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
}

checks = [
    {"name": "uses_module_ui", "passed": has_module_ui},
    {"name": "uses_module_server", "passed": has_module_server},
    {"name": "mounts_distinct_namespaces", "passed": len(namespaces) >= 2},
]
score = sum(c["passed"] for c in checks) / len(checks)
print(json.dumps({"score": score, "success": all(c["passed"] for c in checks), "checks": checks}))
