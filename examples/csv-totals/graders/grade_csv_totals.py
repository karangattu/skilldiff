"""Grade the sum_csv.py task. Prints {"score", "success", "checks"} as JSON.

Runs inside the workspace. Expects `data.csv` (from the fixture) and grades
whatever `sum_csv.py` the agent left behind — written from scratch or fixed.
The expected total is computed with csv + Decimal (the spec), so equivalent
solutions pass and only style rules differentiate the score.
"""
import ast
import csv
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


def expected_total(path: str, column: str) -> Decimal:
    acc = Decimal("0")
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            acc += Decimal(row[column])
    return acc


script = Path("sum_csv.py")
expected = expected_total("data.csv", "amount")

ran = False
out = ""
if script.exists():
    proc = subprocess.run(
        [sys.executable, str(script), "data.csv", "amount"],
        capture_output=True,
        text=True,
    )
    ran = proc.returncode == 0
    out = (proc.stdout or "").strip()

try:
    correct = ran and Decimal(out) == expected
except Exception:
    correct = False

uses_csv = uses_decimal = False
if script.exists():
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except SyntaxError:
        tree = None
    for node in ast.walk(tree) if tree else []:
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            uses_csv = uses_csv or root == "csv"
            uses_decimal = uses_decimal or root == "decimal"

named = [
    ("script exists", script.exists()),
    ("runs without error", ran),
    ("correct total", correct),
    ("plain decimal output", ran and out == str(expected)),
    ("uses csv module", uses_csv),
    ("uses decimal.Decimal", uses_decimal),
]
checks = [{"name": name, "passed": bool(passed)} for name, passed in named]
score = sum(1 for _, passed in named if passed) / len(named)
print(
    json.dumps(
        {"score": score, "success": all(passed for _, passed in named), "checks": checks}
    )
)
