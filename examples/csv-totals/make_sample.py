"""Regenerate the committed sample report from synthetic run records.

The sample shows what a finished skilldiff run looks like without paying for
agent sessions: dev vs held-out splits, a couple of evaluation failures, and
recorded pricing rates. Usage (from this directory, with skilldiff installed):

    python3 make_sample.py

Then `sample/` holds results.json, report.md, report.html, and report.qmd.
The numbers are synthetic; do not quote them as findings.
"""
import json
from pathlib import Path

from skilldiff.reporter import (
    build_html_report,
    build_markdown_report,
    build_quarto_report,
)

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "sample"

MODEL = "claude-sonnet-5"
CHECK_NAMES = [
    "script exists",
    "runs without error",
    "correct total",
    "plain decimal output",
    "uses csv module",
    "uses decimal.Decimal",
]

PRICING = {
    "source": "https://www.anthropic.com/pricing",
    "date": "2026-09-27",
    "currency": "USD",
    "rates": {
        MODEL: {
            "input": 3.00,
            "output": 15.00,
            "cache_read": 0.30,
            "cache_write": 3.75,
        }
    },
}


def make_run(
    arm: str,
    task_id: str,
    split: str,
    rep: int,
    score,
    *,
    status: str = "ok",
    grade_status: str = "graded",
    skill_invoked: bool = False,
    cost: float = 0.42,
    duration: float = 95.0,
    tokens=(45_000, 380_000, 12_000, 6_400),
    turns: int = 14,
) -> dict:
    input_tokens, cache_read, cache_write, output = tokens
    feedback = None
    success = False
    if score is not None:
        passed = round(score * len(CHECK_NAMES))
        feedback = {
            "checks": [
                {"name": name, "passed": i < passed}
                for i, name in enumerate(CHECK_NAMES)
            ]
        }
        success = passed == len(CHECK_NAMES)
    return {
        "complete": True,
        "model": MODEL,
        "task_id": task_id,
        "task_category": "intended",
        "task_split": split,
        "repetition": rep,
        "arm": arm,
        "run_order": 1 if arm == "control" else 2,
        "seed": 1234,
        "attempt": 1,
        "status": status,
        "error": "timeout after 600s" if status == "timeout" else None,
        "duration": duration,
        "cost": cost,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
        "output_tokens": output,
        "num_turns": turns,
        "skill_invoked": skill_invoked if arm == "treatment" else False,
        "skill_available": arm == "treatment",
        "files_changed": ["sum_csv.py"],
        "score": score,
        "success": success,
        "grade_status": grade_status,
        "feedback": feedback,
        "artifacts": f"{MODEL}/{task_id}/{arm}/{rep:03d}",
    }


def build_runs() -> dict:
    control, treatment = [], []
    # Dev pair 3: the control session timed out before grading (agent failure).
    dev = [
        # rep, control score, treatment score
        (1, 2 / 6, 6 / 6),
        (2, 2 / 6, 6 / 6),
        (3, None, 5 / 6),
        (4, 3 / 6, 6 / 6),
    ]
    for rep, c_score, t_score in dev:
        control.append(
            make_run(
                "control",
                "fix-total",
                "dev",
                rep,
                c_score,
                status="timeout" if c_score is None else "ok",
                grade_status="ungraded" if c_score is None else "graded",
                cost=0.38 + 0.03 * rep,
                duration=88.0 + 4 * rep,
                turns=12 + rep,
            )
        )
        treatment.append(
            make_run(
                "treatment",
                "fix-total",
                "dev",
                rep,
                t_score,
                skill_invoked=rep != 2,
                cost=0.31 + 0.02 * rep,
                duration=70.0 + 3 * rep,
                tokens=(38_000, 320_000, 9_000, 5_200),
                turns=10 + rep,
            )
        )
    # Held-out pair 2: the grader itself errored (evaluation failure).
    held = [
        (1, 3 / 6, 5 / 6),
        (2, 3 / 6, None),
        (3, 2 / 6, 3 / 6),
        (4, 3 / 6, 3 / 6),
    ]
    for rep, c_score, t_score in held:
        control.append(
            make_run(
                "control",
                "write-total",
                "held-out",
                rep,
                c_score,
                cost=0.44 + 0.02 * rep,
                duration=92.0 + 3 * rep,
                turns=13 + rep,
            )
        )
        treatment.append(
            make_run(
                "treatment",
                "write-total",
                "held-out",
                rep,
                t_score,
                grade_status="error" if t_score is None else "graded",
                skill_invoked=True,
                cost=0.36 + 0.02 * rep,
                duration=78.0 + 2 * rep,
                tokens=(40_000, 340_000, 10_000, 5_600),
                turns=11 + rep,
            )
        )
    return {"control": control, "treatment": treatment}


def build_results() -> dict:
    return {
        "name": "csv-totals",
        "skilldiff_version": "0.9.1",
        "preset": "skill",
        "arm_labels": {},
        "harness": "claude",
        "skill": "./skills/csv-totals",
        "skill_names": ["csv-totals"],
        "timestamp": "2026-09-27T120000Z",
        "run_dir": None,
        "models": [MODEL],
        "tasks": ["fix-total", "write-total"],
        "task_categories": {"fix-total": "intended", "write-total": "intended"},
        "task_details": [
            {
                "id": "fix-total",
                "category": "intended",
                "split": "dev",
                "repo": "../../fixtures/dev-fix",
                "grader": "python3 ../../graders/grade_csv_totals.py",
            },
            {
                "id": "write-total",
                "category": "intended",
                "split": "held-out",
                "repo": "../../fixtures/heldout-write",
                "grader": "python3 ../../graders/grade_csv_totals.py",
            },
        ],
        "tasks_count": 2,
        "runs_per_arm": 4,
        "seed": 1234,
        "failure_policy": {"agent_failure": "exclude", "missing": "exclude"},
        "valid": True,
        "interrupted": False,
        "warnings": [
            "1 of 8 control runs ended with timeout (agent infrastructure failure, "
            "not a low score); 1 have N/A scores (grading unavailable).",
            "1 of 8 skill runs have grader error; their scores are N/A and excluded "
            "from means. These are evaluation-infra failures, shown with valid-pair counts.",
        ],
        "settings": {"harness": "claude", "timeout_seconds": 600, "parallel": 1},
        "thresholds": {},
        "pricing": PRICING,
        "runs": build_runs(),
    }


def main() -> None:
    results = build_results()
    SAMPLE.mkdir(exist_ok=True)
    (SAMPLE / "results.json").write_text(
        json.dumps(results, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    (SAMPLE / "report.md").write_text(build_markdown_report(results), encoding="utf-8")
    (SAMPLE / "report.html").write_text(build_html_report(results), encoding="utf-8")
    (SAMPLE / "report.qmd").write_text(build_quarto_report(results), encoding="utf-8")
    print(f"Wrote {SAMPLE}/results.json, report.md, report.html, report.qmd")


if __name__ == "__main__":
    main()
