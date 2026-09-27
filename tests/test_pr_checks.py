from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import github_adapter  # noqa: E402
import pr_check_workflow  # noqa: E402
import risk_gate_config  # noqa: E402
import ship_workflow  # noqa: E402


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class PRCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "harness@example.test")
        git(self.root, "config", "user.name", "Harness Test")
        (self.root / "README.md").write_text("fixture\n")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-qm", "fixture")
        git(self.root, "checkout", "-qb", "feature/checks")
        git(self.root, "remote", "add", "origin", "git@github.com:owner/repo.git")
        self.head = git(self.root, "rev-parse", "HEAD")
        self.home = self.parent / "home"
        config_path = self.home / ".config/codex-harness/config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"prChecks": {"required": ["Risk Gate"]}}))
        self.env = patch.dict("os.environ", {"HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def run_data(name: str, *, status: str = "completed", conclusion: str | None = "success",
                 summary: str = "", head_sha: str | None = None, details_url: str = "",
                 external_id: str = "") -> dict[str, object]:
        return {
            "name": name, "status": status, "conclusion": conclusion, "head_sha": head_sha,
            "output": {"summary": summary, "text": ""}, "details_url": details_url,
            "external_id": external_id,
        }

    def inspect(self, runs: list[dict[str, object]], statuses: list[dict[str, object]] | None = None):
        def fake_gh(_root, *args):
            if any("check-runs" in arg for arg in args):
                data = {"check_runs": runs}
            else:
                data = {"statuses": statuses or []}
            return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(data), "")

        with patch.object(pr_check_workflow, "_remote_head", return_value=self.head), \
                patch.object(ship_workflow, "require_github_remote", return_value=("origin", "owner/repo")), \
                patch.object(ship_workflow, "pull_request", return_value={"prNumber": 6}), \
                patch.object(ship_workflow, "github", side_effect=fake_gh):
            return pr_check_workflow.inspect(self.root)

    def test_check_runs_uses_explicit_get_and_accept_header(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_gh(_root, *args):
            calls.append(args)
            data = {"check_runs": []} if any("check-runs" in arg for arg in args) else {"statuses": []}
            return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(data), "")

        with patch.object(ship_workflow, "github", side_effect=fake_gh):
            github_adapter.check_results(self.root, "owner/repo", self.head)

        self.assertEqual(calls[0], (
            "api", "--method", "GET", "-H", "Accept: application/vnd.github+json",
            f"repos/owner/repo/commits/{self.head}/check-runs", "-f", "per_page=100",
        ))

    def test_all_required_checks_success_returns_pass(self) -> None:
        payload, code = self.inspect([self.run_data("Risk Gate", head_sha=self.head)])
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(code, 0)
        self.assertEqual(payload["checks"][0]["conclusion"], "SUCCESS")

    def test_running_and_missing_required_checks_are_pending(self) -> None:
        payload, code = self.inspect([self.run_data("Risk Gate", status="in_progress", conclusion=None, head_sha=self.head)])
        self.assertEqual((payload["status"], code), ("PENDING", 1))
        payload, code = self.inspect([])
        self.assertEqual((payload["status"], code), ("PENDING", 1))

    def test_compile_test_and_convention_failures_are_repairable(self) -> None:
        for name, expected in (("compileKotlin", "COMPILE"), ("unit tests", "TEST"), ("Build Convention", "BUILD_CONVENTION")):
            with self.subTest(name=name):
                config = self.home / ".config/codex-harness/config.json"
                config.write_text(json.dumps({"prChecks": {"required": [name]}}))
                payload, code = self.inspect([self.run_data(name, conclusion="failure", summary=name, head_sha=self.head)])
                self.assertEqual((payload["status"], code), ("REPAIRABLE_FAILURE", 3))
                self.assertIn("failures", payload)

    def test_risk_gate_block_review_and_error_are_distinguished(self) -> None:
        cases = (("BLOCK", "REPAIRABLE_FAILURE", 3), ("REVIEW", "NEEDS_REVIEW", 2), ("ERROR", "ERROR", 4))
        for decision, status, code in cases:
            with self.subTest(decision=decision):
                payload, actual_code = self.inspect([self.run_data("Risk Gate", conclusion="failure", summary=f'{{"decision":"{decision}"}}', head_sha=self.head)])
                self.assertEqual((payload["status"], actual_code), (status, code))

    def test_jev_review_requires_human_review(self) -> None:
        config = self.home / ".config/codex-harness/config.json"
        config.write_text(json.dumps({"prChecks": {"required": ["Jev"]}}))
        payload, code = self.inspect([self.run_data("Jev", conclusion="failure", summary='{"decision":"REVIEW"}', head_sha=self.head)])
        self.assertEqual((payload["status"], code), ("NEEDS_REVIEW", 2))
        payload, code = self.inspect([self.run_data("Jev", summary='{"decision":"REVIEW"}', head_sha=self.head)])
        self.assertEqual((payload["status"], code), ("NEEDS_REVIEW", 2))

    def test_mixed_failures_all_appear_in_repair_packet(self) -> None:
        config = self.home / ".config/codex-harness/config.json"
        config.write_text(json.dumps({"prChecks": {"required": ["compile", "tests"]}}))
        runs = [
            self.run_data("compile", conclusion="failure", summary="compileJava failed", head_sha=self.head),
            self.run_data("tests", conclusion="failure", summary="test:ExampleTest failed", head_sha=self.head),
        ]
        payload, code = self.inspect(runs)
        self.assertEqual((payload["status"], code), ("REPAIRABLE_FAILURE", 3))
        self.assertEqual([item["check"] for item in payload["failures"]], ["compile", "tests"])

    def test_sha_mismatch_never_passes(self) -> None:
        with patch.object(pr_check_workflow, "_remote_head", return_value="f" * 40):
            with patch.object(ship_workflow, "require_github_remote", return_value=("origin", "owner/repo")):
                with self.assertRaisesRegex(pr_check_workflow.PRCheckError, "remote branch"):
                    pr_check_workflow.inspect(self.root)
        mismatched = self.run_data("Risk Gate", head_sha="f" * 40)
        def fake_gh(_root, *args):
            data = {"check_runs": [mismatched]} if any("check-runs" in arg for arg in args) else {"statuses": []}
            return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(data), "")
        with patch.object(ship_workflow, "github", side_effect=fake_gh):
            _rows, matches = github_adapter.check_results(self.root, "owner/repo", self.head)
        self.assertFalse(matches)

    def test_pr_missing_is_an_error(self) -> None:
        with patch.object(pr_check_workflow, "_remote_head", return_value=self.head), \
                patch.object(ship_workflow, "require_github_remote", return_value=("origin", "owner/repo")), \
                patch.object(ship_workflow, "pull_request", side_effect=ship_workflow.ShipError("No PR", category="PR_NOT_FOUND")):
            with self.assertRaisesRegex(ship_workflow.ShipError, "No PR"):
                pr_check_workflow.inspect(self.root)

    def test_github_auth_failure_does_not_expose_stderr_secret(self) -> None:
        secret = "ghp_abcdefghijklmnopqrstuv123456"
        with patch.object(ship_workflow, "github", return_value=subprocess.CompletedProcess([], 1, "", f"auth failed {secret}")):
            with self.assertRaises(github_adapter.GitHubError):
                github_adapter.check_results(self.root, "owner/repo", self.head)
        safe = github_adapter._safe_text(f"Authorization: Bearer {secret}")
        self.assertNotIn(secret, safe)

    def test_config_validation_rejects_missing_empty_and_duplicate_names(self) -> None:
        for value in ({}, {"required": []}, {"required": ["Risk Gate", "Risk Gate"]}, {"required": [""]}):
            with self.subTest(value=value):
                with self.assertRaises(risk_gate_config.ConfigurationError):
                    risk_gate_config.validate_pr_checks({"prChecks": value})


if __name__ == "__main__":
    unittest.main()
