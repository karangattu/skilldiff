import ast
import json
from pathlib import Path

src = Path("app.py").read_text(encoding="utf-8")
tree = ast.parse(src)

has_inplace_mutation = any(
    isinstance(n, ast.Call)
    and isinstance(n.func, ast.Attribute)
    and n.func.attr in {"append", "extend", "insert"}
    and isinstance(n.func.value, ast.Call)
    and isinstance(n.func.value.func, ast.Name)
    and n.func.value.func.id == "items"
    for n in ast.walk(tree)
)
has_items_set = any(
    isinstance(n, ast.Call)
    and isinstance(n.func, ast.Attribute)
    and n.func.attr == "set"
    and isinstance(n.func.value, ast.Name)
    and n.func.value.id == "items"
    for n in ast.walk(tree)
)
keeps_ids = all(tok in src for tok in ('"item_name"', '"add_btn"', '"count_txt"'))

checks = [
    {"name": "no_inplace_mutation", "passed": not has_inplace_mutation},
    {"name": "calls_items_set", "passed": has_items_set},
    {"name": "preserves_ids", "passed": keeps_ids},
]
score = sum(c["passed"] for c in checks) / len(checks)
print(json.dumps({"score": score, "success": all(c["passed"] for c in checks), "checks": checks}))
