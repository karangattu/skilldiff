import ast
import json
from pathlib import Path

src = Path("app.py").read_text(encoding="utf-8")
tree = ast.parse(src)

imports_express = any(
    (isinstance(n, ast.ImportFrom) and n.module and "express" in n.module)
    or (isinstance(n, ast.Import) and any("express" in a.name for a in n.names))
    for n in ast.walk(tree)
)
has_core_app = any(
    isinstance(n, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id == "app" for t in n.targets)
    and isinstance(n.value, ast.Call)
    for n in tree.body
)
has_greeting = any(
    isinstance(n, ast.FunctionDef) and n.name == "greeting"
    for n in ast.walk(tree)
)

checks = [
    {"name": "no_shiny_express_import", "passed": not imports_express},
    {"name": "defines_core_app", "passed": has_core_app},
    {"name": "defines_greeting_output", "passed": has_greeting},
]
score = sum(c["passed"] for c in checks) / len(checks)
print(json.dumps({"score": score, "success": all(c["passed"] for c in checks), "checks": checks}))
