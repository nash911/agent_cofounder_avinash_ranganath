"""End-to-end tests for :mod:`harness.eval`, plus unit tests for its metrics.

``python3 -m harness.eval`` is driven as a real subprocess, exactly the way
``support.py`` drives the harness, with ``--challenge-command`` pointed at a
stub that writes the files a real run leaves behind. Two hard rules are
enforced by construction: the stub never makes a model call or touches port
3000, and ``--repo-root`` points at a scratch tree that mimics the repository
layout, so the real working tree is never written to.
"""

from __future__ import annotations

import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from harness import eval_metrics
from harness.tests import support

#: A stand-in for ``npm run challenge``. Written to the scratch tree by
#: ``setUp`` and invoked through ``--challenge-command``.
STUB_SOURCE = r'''#!/usr/bin/env python3
"""A deterministic stand-in for ``npm run challenge``. **Test use only.**

No model, no network, no port 3000. It writes exactly the artifacts the real
runner leaves behind -- ``result.json`` at the repository root, the mirrors
under ``output/app``, a generated ``src/`` tree, and a fresh
``artifacts/runs/<id>`` -- so the snapshot and metric code can be exercised
offline. Environment knobs: STUB_SLEEP, STUB_GATE, STUB_NO_RESULT, STUB_LEAK,
STUB_INPUT_TOKENS, STUB_OUTPUT_TOKENS, STUB_CACHE_TOKENS.
"""

import json
import os
import pathlib
import shutil
import sys
import time

CONFIG = """import { defineApp } from "./lib/config-types.js";

export const LOW = 2;

export const appConfig = defineApp({
  storageKey: "notes.v1",
  fields: [
    { kind: "text", name: "title", label: "Title", required: true },
    { kind: "select", name: "kind", label: "Kind", options: ["A", "B"] },
    { kind: "number", name: "count", label: "Count", min: 0, integer: true },
  ],
  filters: [
    { kind: "field", field: "kind", allLabel: "All" },
    { kind: "state", id: "low", label: "Low", match: (row) => row.count <= LOW },
  ],
  badges: [
    { id: "low", when: (row) => row.count <= LOW, tone: "alert",
      text: (row) => `Low [${row.count}]` },
  ],
  summary: [
    { id: "total", label: "Notes", compute: (rows) => rows.length },
  ],
  actions: [
    { id: "bump", label: "Bump", apply: (row) => ({ count: row.count + 1 }) },
  ],
});
"""

TESTS = """import { describe, expect, it } from "vitest";

describe("journeys", () => {
  it("adds a note and shows it", async () => {
    expect(1).toBe(1);
    expect(2).toBe(2);
  });

  it("edits a note", async () => {
    expect(3).toBe(3);
  });
});
"""


def main() -> int:
    repo = pathlib.Path.cwd()
    argv = sys.argv[1:]
    idea = argv[argv.index("--idea-file") + 1] if "--idea-file" in argv else ""
    sys.stdout.write("stub challenge for " + idea + "\n")
    sys.stderr.write("stub stderr\n")

    sleep_s = float(os.environ.get("STUB_SLEEP") or 0)
    if sleep_s > 0:
        time.sleep(sleep_s)

    app = repo / "output" / "app"
    app.mkdir(parents=True, exist_ok=True)
    if (app / "src").exists():
        shutil.rmtree(str(app / "src"))
    shutil.copytree(str(repo / "app-template" / "src"), str(app / "src"))
    (app / "src" / "app-config.ts").write_text(CONFIG, encoding="utf-8")
    tests = TESTS
    if os.environ.get("STUB_LEAK") == "1":
        tests += "// localStorage.getItem('notes.v1');\n"
    (app / "src" / "journeys.test.tsx").write_text(tests, encoding="utf-8")
    (app / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    (app / "report.partial.json").write_text(
        json.dumps({"status": "success", "summary": "stub"}) + "\n", encoding="utf-8"
    )

    run_dir = repo / "artifacts" / "runs" / ("2026-09-04T00-00-00-" + str(time.time_ns())[-9:] + "Z")
    (run_dir / "harness").mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text('{"type":"message_end"}\n', encoding="utf-8")
    (run_dir / "idea.txt").write_text(idea + "\n", encoding="utf-8")
    (run_dir / "harness" / "spec.json").write_text(
        json.dumps({"journeys": [1, 2], "fields": [1, 2, 3]}), encoding="utf-8"
    )
    (run_dir / "harness" / "supervisor.json").write_text(
        json.dumps({"repairs": 1, "final_action": "done"}), encoding="utf-8"
    )
    (run_dir / "harness" / "missions.json").write_text(
        json.dumps({"sessions": ["1-builder", "2-tester"]}), encoding="utf-8"
    )

    if os.environ.get("STUB_NO_RESULT") == "1":
        return 1

    green = os.environ.get("STUB_GATE", "pass") == "pass"
    verdict = "passed" if green else "failed"
    result = {
        "status": "success" if green else "partial",
        "app_url": "http://localhost:3000",
        "start_command": "npm --prefix 'output/app' run dev",
        "summary": "A stub app.",
        "implemented_features": ["Adds a note"],
        "assumptions": [],
        "tests_run": [
            {"command": "npm test", "journey": "adds a note", "result": "passed"},
            {"command": "npm test", "journey": "edits a note", "result": "passed"},
        ],
        "harness_checks": [
            {"command": "vitest run", "journey": "tests", "result": verdict},
            {"command": "npm run build", "journey": "build", "result": "passed"},
            {"command": "npm run dev", "journey": "dev server", "result": "passed"},
        ],
        "model_calls": 7,
        "input_tokens": int(os.environ.get("STUB_INPUT_TOKENS") or 1000),
        "output_tokens": int(os.environ.get("STUB_OUTPUT_TOKENS") or 500),
        "cache_read_tokens": int(os.environ.get("STUB_CACHE_TOKENS") or 10000),
        "cache_write_tokens": 0,
        "total_tokens": 11500,
        "reasoning_tokens": 0,
        "cost_total": 0.0625,
        "call_log": [],
        "pi_exit_code": 0,
        "telemetry_source": "pi-json-event-stream",
        "port_reclamation": {
            "preexisting_listener": False,
            "listener_after_pi": False,
            "attempted": False,
            "reclaimed": False,
            "process_ids": [],
            "diagnostic": "Port 3000 remained free after the stub",
        },
    }
    body = json.dumps(result, indent=2) + "\n"
    (repo / "result.json").write_text(body, encoding="utf-8")
    (app / "result.json").write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

TEMPLATE_CONFIG = 'import { defineApp } from "./lib/config-types.js";\n\nexport const appConfig = defineApp({\n  fields: [],\n});\n'
PRE_EXISTING_RUN = "2026-09-01T00-00-00-000Z"


class EvalRunnerTest(unittest.TestCase):
    """Drives ``python3 -m harness.eval`` against a scratch repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.cases = self.root / "cases"
        self.output = self.root / "out"
        self.reports = self.root / "reports"
        self.stub = self.root / "stub_challenge.py"

        template = self.repo / "app-template" / "src"
        (template / "components").mkdir(parents=True)
        (template / "lib").mkdir(parents=True)
        (template / "app-config.ts").write_text(TEMPLATE_CONFIG, encoding="utf-8")
        (template / "components" / "EmptyState.tsx").write_text(
            'export const EmptyState = () => <p aria-label="empty" />;\n', encoding="utf-8"
        )
        (template / "components" / "ErrorBoundary.tsx").write_text(
            "export class ErrorBoundary {}\n", encoding="utf-8"
        )
        (template / "lib" / "repository.ts").write_text(
            "export const store = localStorage;\n", encoding="utf-8"
        )
        (self.repo / "output" / "app").mkdir(parents=True)
        (self.repo / "artifacts" / "runs" / PRE_EXISTING_RUN).mkdir(parents=True)
        self.cases.mkdir()
        (self.cases / "notes.txt").write_text("An app for keeping notes.\n", encoding="utf-8")
        self.stub.write_text(STUB_SOURCE, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def run_eval(self, *extra, env_extra=None, repeats=1, wait_s=180.0):
        argv = [
            sys.executable, "-m", "harness.eval",
            "--cases", str(self.cases),
            "--output-root", str(self.output),
            "--report-dir", str(self.reports),
            "--repo-root", str(self.repo),
            "--repeats", str(repeats),
            "--challenge-command",
            "{0} {1}".format(shlex.quote(sys.executable), shlex.quote(str(self.stub))),
        ]
        argv.extend(extra)
        return subprocess.run(
            argv,
            cwd=str(support.REPO_ROOT),
            env=support.harness_environment(env_extra),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=wait_s,
        )

    def latest_report(self, directory=None):
        reports = sorted((directory or self.reports).glob("eval-*.json"))
        self.assertTrue(reports, "no eval-*.json report was written")
        return json.loads(reports[-1].read_text(encoding="utf-8"))

    # -- argument validation ---------------------------------------------

    def test_cases_inside_the_repository_are_rejected(self):
        inside = self.repo / "holdout"
        inside.mkdir()
        (inside / "notes.txt").write_text("leaked\n", encoding="utf-8")
        done = self.run_eval("--cases", str(inside))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("outside the repository", done.stderr)
        self.assertFalse(list(self.reports.glob("*")) if self.reports.is_dir() else [])

    def test_relative_cases_path_is_rejected(self):
        done = self.run_eval("--cases", "cases")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("absolute path", done.stderr)

    def test_relative_output_root_is_rejected(self):
        done = self.run_eval("--output-root", "out")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("absolute path", done.stderr)

    def test_missing_cases_path_is_rejected(self):
        done = self.run_eval("--cases", str(self.root / "nowhere"))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("does not exist", done.stderr)

    def test_report_dir_inside_the_repository_is_rejected(self):
        done = self.run_eval("--report-dir", str(self.repo / "reports"))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("outside the repository", done.stderr)

    # -- snapshot and cleanup --------------------------------------------

    def test_snapshot_holds_every_artifact_of_the_run(self):
        done = self.run_eval()
        self.assertEqual(done.returncode, 0, done.stderr)
        snapshot = self.output / "notes" / "1"
        for relative in (
            "result.json",
            "app.result.json",
            "report.partial.json",
            "AGENTS.md",
            "challenge.stdout.log",
            "challenge.stderr.log",
            "app-src/app-config.ts",
            "app-src/journeys.test.tsx",
            "app-src/lib/repository.ts",
            "run/events.jsonl",
            "run/harness/spec.json",
            "run/harness/supervisor.json",
            "run/harness/missions.json",
        ):
            self.assertTrue((snapshot / relative).exists(), "missing {0}".format(relative))
        self.assertIn("stub challenge", (snapshot / "challenge.stdout.log").read_text())
        self.assertIn("stub stderr", (snapshot / "challenge.stderr.log").read_text())

    def test_run_directory_is_moved_out_and_root_result_deleted(self):
        done = self.run_eval()
        self.assertEqual(done.returncode, 0, done.stderr)
        remaining = sorted(p.name for p in (self.repo / "artifacts" / "runs").iterdir())
        self.assertEqual(remaining, [PRE_EXISTING_RUN], "the run directory was not moved out")
        self.assertFalse((self.repo / "result.json").exists(), "root result.json survived")
        # output/app is the runner's to re-seed; the evaluator must not delete it.
        self.assertTrue((self.repo / "output" / "app").is_dir())
        report = self.latest_report()
        self.assertTrue(report["runs"][0]["run_dir"].endswith("Z"))

    # -- metrics ----------------------------------------------------------

    def test_metrics_are_computed_from_the_snapshot(self):
        done = self.run_eval(env_extra={"STUB_INPUT_TOKENS": "2000", "STUB_OUTPUT_TOKENS": "1000"})
        self.assertEqual(done.returncode, 0, done.stderr)
        run = self.latest_report()["runs"][0]

        self.assertTrue(run["gate"])
        self.assertEqual(run["gate_reason"], "")
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["model_calls"], 7)
        # 2000 input + 3 x 1000 output + 0.1 x 10000 cache read.
        self.assertEqual(run["points"], 6000.0)
        self.assertEqual(run["tests_run"], 2)
        self.assertEqual(run["tests_failed"], 0)
        self.assertGreater(run["wall_s"], 0.0)

        self.assertTrue(run["config_present"])
        self.assertEqual(run["fields"], 3)
        self.assertEqual(run["filters"], 2)
        self.assertEqual(run["badges"], 1)
        self.assertEqual(run["summary"], 1)
        self.assertEqual(run["actions"], 1)
        self.assertEqual(run["exported_consts"], 2)
        self.assertEqual(run["number_fields"], 1)
        self.assertTrue(run["numeric_validation"])

        self.assertEqual(run["test_its"], 2)
        self.assertEqual(run["test_expects"], 3)
        self.assertEqual(run["assertion_density"], 1.5)

        # app-config.ts was rewritten and journeys.test.tsx added; nothing else.
        self.assertEqual(sorted(run["changed_files"]), ["app-config.ts", "journeys.test.tsx"])
        self.assertEqual(run["changed_file_count"], 2)
        self.assertFalse(run["localstorage_outside_repository"])
        self.assertTrue(run["has_empty_state"])
        self.assertTrue(run["has_error_boundary"])
        self.assertTrue(run["has_aria_label"])

        self.assertEqual(run["spec_journeys"], 2)
        self.assertEqual(run["spec_fields"], 3)
        self.assertEqual(run["repairs"], 1)
        self.assertEqual(run["final_action"], "done")
        self.assertEqual(run["sessions"], 2)
        self.assertTrue(run["report_partial_present"])

    def test_repeats_are_honoured(self):
        done = self.run_eval(repeats=2)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.output / "notes" / "1" / "result.json").is_file())
        self.assertTrue((self.output / "notes" / "2" / "result.json").is_file())
        report = self.latest_report()
        self.assertEqual(len(report["runs"]), 2)
        self.assertEqual([run["repeat"] for run in report["runs"]], [1, 2])
        self.assertEqual(report["cases"]["notes"]["runs"], 2)
        self.assertEqual(report["cases"]["notes"]["gate_pass_rate"], 1.0)

    # -- the report -------------------------------------------------------

    def test_report_json_and_markdown_are_written_and_printed(self):
        done = self.run_eval("--label", "phase-4 smoke")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(list(self.reports.glob("eval-*.json"))), 1)
        markdown = sorted(self.reports.glob("eval-*.md"))
        self.assertEqual(len(markdown), 1)
        body = markdown[0].read_text(encoding="utf-8")
        self.assertIn("## Per case", body)
        self.assertIn("## Every run", body)
        self.assertIn("notes", body)
        self.assertIn("all 1 runs passed the gate", body)
        # The same table reaches stdout for whoever is watching the terminal.
        self.assertIn("## Per case", done.stdout)
        self.assertIn("phase-4 smoke", done.stdout)
        report = self.latest_report()
        self.assertEqual(report["label"], "phase-4 smoke")
        self.assertEqual(report["schema"], "agentcofounder.eval.v1")
        self.assertNotIn("_markdown", report)

    def test_a_failed_gate_exits_one(self):
        done = self.run_eval(env_extra={"STUB_GATE": "fail"})
        self.assertEqual(done.returncode, 1, done.stderr)
        report = self.latest_report()
        self.assertFalse(report["runs"][0]["gate"])
        self.assertEqual(len(report["gate_failures"]), 1)
        self.assertIn("gate failure", done.stderr)
        self.assertIn("**FAIL**", done.stdout)

    def test_a_missing_result_file_fails_the_gate(self):
        done = self.run_eval(env_extra={"STUB_NO_RESULT": "1"})
        self.assertEqual(done.returncode, 1, done.stderr)
        run = self.latest_report()["runs"][0]
        self.assertFalse(run["gate"])
        self.assertEqual(run["exit_code"], 1)
        self.assertIn("missing", run["gate_reason"])

    def test_baseline_regression_exits_one_with_a_reason(self):
        first = self.run_eval()
        self.assertEqual(first.returncode, 0, first.stderr)
        baseline = sorted(self.reports.glob("eval-*.json"))[-1]

        second_reports = self.root / "reports2"
        worse = self.run_eval(
            "--baseline", str(baseline),
            "--report-dir", str(second_reports),
            env_extra={"STUB_OUTPUT_TOKENS": "2000"},
        )
        self.assertEqual(worse.returncode, 1, worse.stderr)
        report = self.latest_report(second_reports)
        self.assertEqual(len(report["regressions"]), 1)
        self.assertIn("mean points rose", report["regressions"][0])
        self.assertIn("regression", worse.stderr)
        self.assertEqual(report["gate_failures"], [])

    def test_baseline_without_regression_exits_zero(self):
        first = self.run_eval()
        self.assertEqual(first.returncode, 0, first.stderr)
        baseline = sorted(self.reports.glob("eval-*.json"))[-1]
        third_reports = self.root / "reports3"
        again = self.run_eval("--baseline", str(baseline), "--report-dir", str(third_reports))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.latest_report(third_reports)["regressions"], [])

    def test_a_baseline_that_is_not_a_report_is_a_usage_error(self):
        junk = self.root / "junk.json"
        junk.write_text("{}\n", encoding="utf-8")
        done = self.run_eval("--baseline", str(junk))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("not an eval report", done.stderr)

    # -- timeout ----------------------------------------------------------

    def test_a_timeout_kills_the_process_group(self):
        started = time.monotonic()
        done = self.run_eval("--timeout-s", "2", env_extra={"STUB_SLEEP": "120"}, wait_s=90.0)
        elapsed = time.monotonic() - started
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertLess(elapsed, 60.0, "the runner waited for the stub instead of killing it")
        run = self.latest_report()["runs"][0]
        self.assertTrue(run["timed_out"])
        self.assertFalse(run["gate"])
        self.assertLess(run["wall_s"], 30.0)


class EvalMetricsTest(unittest.TestCase):
    """Unit tests for the pure functions -- no subprocess, no scratch repo."""

    def test_points_weights(self):
        self.assertEqual(eval_metrics.points(1000, 500, 10000), 3500.0)
        self.assertEqual(eval_metrics.points(0, 0, 0), 0.0)

    def test_gate_requires_success_and_passing_checks(self):
        good = {"status": "success", "harness_checks": [{"result": "passed"}]}
        self.assertEqual(eval_metrics.gate(good), (True, ""))

        passed, reason = eval_metrics.gate({"status": "partial", "harness_checks": []})
        self.assertFalse(passed)
        self.assertIn("partial", reason)

        passed, reason = eval_metrics.gate({"status": "success", "harness_checks": []})
        self.assertFalse(passed)
        self.assertIn("never verified", reason)

        passed, reason = eval_metrics.gate(
            {"status": "success", "harness_checks": [
                {"result": "passed", "journey": "tests"},
                {"result": "failed", "journey": "dev server"},
            ]}
        )
        self.assertFalse(passed)
        self.assertIn("dev server", reason)

        passed, reason = eval_metrics.gate(None)
        self.assertFalse(passed)
        self.assertIn("missing", reason)

    def test_array_section_survives_brackets_inside_strings(self):
        text = 'x\n  badges: [\n    { id: "a", text: (r) => `Low [${r.n}]`, alt: "b]c" },\n  ],\n'
        self.assertEqual(eval_metrics.count_entries(text, "badges"), 1)
        self.assertIsNone(eval_metrics.array_section(text, "filters"))
        self.assertEqual(eval_metrics.count_entries(text, "filters"), 0)

    def test_config_metrics_count_number_validation(self):
        text = (
            "export const LOW = 2;\n"
            "export const appConfig = defineApp({\n"
            "  fields: [\n"
            '    { kind: "text", name: "t" },\n'
            '    { kind: "number", name: "n", min: 0, integer: true },\n'
            '    { kind: "number", name: "m" },\n'
            "  ],\n"
            "});\n"
        )
        metrics = eval_metrics.config_metrics(text)
        self.assertEqual(metrics["fields"], 3)
        self.assertEqual(metrics["number_fields"], 2)
        self.assertEqual(metrics["number_fields_validated"], 1)
        self.assertTrue(metrics["numeric_validation"])
        self.assertEqual(metrics["exported_consts"], 2)

    def test_config_metrics_tolerate_a_missing_file(self):
        metrics = eval_metrics.config_metrics(None)
        self.assertFalse(metrics["config_present"])
        self.assertEqual(metrics["fields"], 0)
        self.assertEqual(metrics["config_lines"], 0)

    def test_tests_metrics_density(self):
        text = 'it("a", () => { expect(1).toBe(1); expect(2).toBe(2); });\nit("b", () => {});\n'
        metrics = eval_metrics.tests_metrics(text)
        self.assertEqual(metrics["test_its"], 2)
        self.assertEqual(metrics["test_expects"], 2)
        self.assertEqual(metrics["assertion_density"], 1.0)

    def test_tree_metrics_flag_a_storage_leak_only_in_changed_files(self):
        with tempfile.TemporaryDirectory(dir=str(support.scratch_root())) as tmp:
            root = pathlib.Path(tmp)
            template = root / "template"
            generated = root / "generated"
            (template / "lib").mkdir(parents=True)
            (generated / "lib").mkdir(parents=True)
            # The scaffold's own storage adapter uses localStorage by design.
            for base in (template, generated):
                (base / "lib" / "repository-storage.ts").write_text(
                    "localStorage.getItem('x');\n", encoding="utf-8"
                )
            (template / "app-config.ts").write_text("seed\n", encoding="utf-8")
            (generated / "app-config.ts").write_text("generated\n", encoding="utf-8")

            metrics = eval_metrics.tree_metrics(generated, template)
            self.assertEqual(metrics["changed_files"], ["app-config.ts"])
            self.assertFalse(metrics["localstorage_outside_repository"])

            (generated / "journeys.test.tsx").write_text(
                "localStorage.clear();\n", encoding="utf-8"
            )
            metrics = eval_metrics.tree_metrics(generated, template)
            self.assertTrue(metrics["localstorage_outside_repository"])
            self.assertEqual(metrics["localstorage_files"], ["journeys.test.tsx"])

    def test_harness_metrics_default_to_none_when_absent(self):
        with tempfile.TemporaryDirectory(dir=str(support.scratch_root())) as tmp:
            metrics = eval_metrics.harness_metrics(pathlib.Path(tmp) / "harness")
        self.assertIsNone(metrics["spec_journeys"])
        self.assertIsNone(metrics["repairs"])
        self.assertIsNone(metrics["sessions"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
