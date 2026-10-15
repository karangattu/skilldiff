import ast
import json
from pathlib import Path

src = Path("app.py").read_text(encoding="utf-8")
tree = ast.parse(src)

has_module_ui = False
has_module_server = False
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
                elif dec.attr == "server":
                    has_module_server = True

has_both_namespaces = '"alpha"' in src and '"beta"' in src

checks = [
    {"name": "uses_module_ui", "passed": has_module_ui},
    {"name": "uses_module_server", "passed": has_module_server},
    {"name": "mounts_alpha_and_beta", "passed": has_both_namespaces},
]
score = sum(c["passed"] for c in checks) / len(checks)
print(json.dumps({"score": score, "success": all(c["passed"] for c in checks), "checks": checks}))
