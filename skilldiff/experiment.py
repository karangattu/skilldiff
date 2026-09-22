import json
import platform
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skilldiff.config import ExperimentConfig, TaskConfig
from skilldiff.grader import Grader
from skilldiff.reporter import calculate_metrics, create_quarto_report
from skilldiff.runner import AgentRunner
from skilldiff.workspace import Workspace


class ExperimentRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        tasks: list[TaskConfig],
        output_dir: Path | None = None,
        agent_runner: AgentRunner | None = None,
    ):
        self.config = config
        self.tasks = tasks
        self.output_dir = output_dir or Path("runs")
        self.agent_runner = agent_runner or AgentRunner()

    def run(self) -> dict[str, Any]:
        timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        run_root = self.output_dir / timestamp_str
        run_root.mkdir(parents=True, exist_ok=True)

        exp_metadata = {
            "name": self.config.name,
            "skill": str(self.config.skill),
            "harness": self.config.harness,
            "models": self.config.models,
            "runs": self.config.runs,
            "claude": asdict(self.config.claude),
            "codex": asdict(self.config.codex),
            "opencode": asdict(self.config.opencode),
            "antigravity": asdict(self.config.antigravity),
            "timestamp": timestamp_str,
            "system": {
                "os": platform.system(),
                "python": platform.python_version(),
            },
        }
        with open(run_root / "experiment.json", "w", encoding="utf-8") as f:
            json.dump(exp_metadata, f, indent=2)

        results_by_model: dict[str, dict[str, Any]] = {}
        all_control_runs: list[dict[str, Any]] = []
        all_treatment_runs: list[dict[str, Any]] = []

        for model in self.config.models:
            model_control_runs: list[dict[str, Any]] = []
            model_treatment_runs: list[dict[str, Any]] = []

            for task in self.tasks:
                grader = Grader(task.grader, self.config.skill.name, model)
                fixture_repo = (
                    (task.source_path.parent / task.repo).resolve()
                    if task.repo and task.source_path
                    else None
                )

                for rep in range(1, self.config.runs + 1):
                    rep_str = f"{rep:03d}"
                    ctrl_dir = run_root / model / task.id / "control" / rep_str
                    treat_dir = run_root / model / task.id / "treatment" / rep_str
                    ctrl_dir.mkdir(parents=True, exist_ok=True)
                    treat_dir.mkdir(parents=True, exist_ok=True)

                    with (
                        tempfile.TemporaryDirectory() as tmp_ctrl,
                        tempfile.TemporaryDirectory() as tmp_treat,
                    ):
                        ws_ctrl = Workspace(
                            root=Path(tmp_ctrl),
                            is_treatment=False,
                            skill_dir=self.config.skill,
                            fixture_repo=fixture_repo,
                            harness=self.config.harness,
                        )
                        ws_treat = Workspace(
                            root=Path(tmp_treat),
                            is_treatment=True,
                            skill_dir=self.config.skill,
                            fixture_repo=fixture_repo,
                            harness=self.config.harness,
                        )

                        ws_ctrl.setup()
                        ws_treat.setup()

                        res_ctrl = self.agent_runner.run(
                            task.prompt, ws_ctrl.root, model, self.config
                        )
                        res_treat = self.agent_runner.run(
                            task.prompt, ws_treat.root, model, self.config
                        )

                        diff_ctrl, files_ctrl = ws_ctrl.get_diff()
                        diff_treat, files_treat = ws_treat.get_diff()

                        grade_ctrl, grade_treat = grader.grade_pair(
                            control_ws=ws_ctrl.root,
                            treatment_ws=ws_treat.root,
                            control_response=res_ctrl.response,
                            treatment_response=res_treat.response,
                            control_diff=diff_ctrl,
                            treatment_diff=diff_treat,
                            control_transcript=res_ctrl.transcript,
                            treatment_transcript=res_treat.transcript,
                        )

                        run_record_ctrl = {
                            "model": model,
                            "task_id": task.id,
                            "repetition": rep,
                            "arm": "control",
                            "prompt": res_ctrl.prompt,
                            "response": res_ctrl.response,
                            "duration": res_ctrl.duration,
                            "cost": res_ctrl.cost,
                            "input_tokens": res_ctrl.input_tokens,
                            "output_tokens": res_ctrl.output_tokens,
                            "tool_calls": res_ctrl.tool_calls,
                            "files_changed": files_ctrl,
                            "score": grade_ctrl.score,
                            "success": grade_ctrl.success,
                            "blind_label": grade_ctrl.label,
                            "feedback": grade_ctrl.feedback,
                            "exit_code": res_ctrl.exit_code,
                        }

                        run_record_treat = {
                            "model": model,
                            "task_id": task.id,
                            "repetition": rep,
                            "arm": "treatment",
                            "prompt": res_treat.prompt,
                            "response": res_treat.response,
                            "duration": res_treat.duration,
                            "cost": res_treat.cost,
                            "input_tokens": res_treat.input_tokens,
                            "output_tokens": res_treat.output_tokens,
                            "tool_calls": res_treat.tool_calls,
                            "files_changed": files_treat,
                            "score": grade_treat.score,
                            "success": grade_treat.success,
                            "blind_label": grade_treat.label,
                            "feedback": grade_treat.feedback,
                            "exit_code": res_treat.exit_code,
                        }

                        self._save_run_artifacts(
                            ctrl_dir, run_record_ctrl, res_ctrl.transcript, diff_ctrl
                        )
                        self._save_run_artifacts(
                            treat_dir, run_record_treat, res_treat.transcript, diff_treat
                        )

                        model_control_runs.append(run_record_ctrl)
                        model_treatment_runs.append(run_record_treat)
                        all_control_runs.append(run_record_ctrl)
                        all_treatment_runs.append(run_record_treat)

            c_metrics = calculate_metrics(model_control_runs)
            s_metrics = calculate_metrics(model_treatment_runs)
            task_metrics: dict[str, dict[str, Any]] = {}
            for task in self.tasks:
                task_control_runs = [
                    run for run in model_control_runs if run["task_id"] == task.id
                ]
                task_treatment_runs = [
                    run for run in model_treatment_runs if run["task_id"] == task.id
                ]
                task_metrics[task.id] = {
                    "control": calculate_metrics(task_control_runs),
                    "skill": calculate_metrics(task_treatment_runs),
                }
            results_by_model[model] = {
                "control": c_metrics,
                "skill": s_metrics,
                "runs_count": len(model_control_runs),
                "by_task": task_metrics,
                "runs": {
                    "control": model_control_runs,
                    "treatment": model_treatment_runs,
                },
            }

        overall_control = calculate_metrics(all_control_runs)
        overall_skill = calculate_metrics(all_treatment_runs)

        full_results = {
            "name": self.config.name,
            "harness": self.config.harness,
            "timestamp": timestamp_str,
            "run_dir": str(run_root),
            "models": self.config.models,
            "tasks_count": len(self.tasks),
            "runs_per_arm": self.config.runs,
            "by_model": results_by_model,
            "overall": {
                "control": overall_control,
                "skill": overall_skill,
            },
            "runs": {
                "control": all_control_runs,
                "treatment": all_treatment_runs,
            },
        }

        qmd_path, html_path = create_quarto_report(full_results, run_root)
        full_results["report"] = {
            "qmd": str(qmd_path),
            "html": str(html_path) if html_path else None,
        }

        with open(run_root / "results.json", "w", encoding="utf-8") as f:
            json.dump(full_results, f, indent=2)

        return full_results

    def _save_run_artifacts(
        self,
        arm_dir: Path,
        record: dict[str, Any],
        transcript: str,
        diff: str,
    ) -> None:
        with open(arm_dir / "run.json", "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        with open(arm_dir / "transcript.txt", "w", encoding="utf-8") as f:
            f.write(transcript)
        with open(arm_dir / "diff.patch", "w", encoding="utf-8") as f:
            f.write(diff)
