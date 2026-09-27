from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "bin"))
import stop_verify  # noqa: E402
import turn_start  # noqa: E402
import risk_gate_config  # noqa: E402
import ship_workflow  # noqa: E402


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git_output(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def make_repo(parent: Path) -> Path:
    root = parent / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "harness@example.test")
    git(root, "config", "user.name", "Harness Test")
    (root / "README.md").write_text("start\n")
    (root / "Main.java").write_text("class Main {}\n")
    (root / "gradlew").write_text("#!/bin/sh\nexit 0\n")
    (root / "gradlew").chmod(0o755)
    (root / "build.gradle").write_text("plugins { id 'io.github.dochiri0916.build-convention' }\n")
    report = root / "build/reports/build-convention/report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}")
    git(root, "add", "README.md", "Main.java", "gradlew", "build.gradle", "build/reports/build-convention/report.json")
    git(root, "commit", "-qm", "initial")
    return root


def invoke_stop(root: Path) -> dict[str, object]:
    from io import StringIO
    import contextlib

    key = hashlib.sha256(b"session-1").hexdigest()
    active = Path(os.environ["CODEX_HARNESS_STATE_DIR"]) / f"{key}.active"
    turn_id = "turn-1"
    if active.is_file():
        state_path = active.parent / active.read_text(encoding="utf-8").strip()
        if state_path.is_file():
            turn_id = json.loads(state_path.read_text(encoding="utf-8"))["turn_id"]
    event = {"session_id": "session-1", "turn_id": turn_id, "cwd": str(root)}
    output = StringIO()
    with patch.object(sys, "stdin", StringIO(json.dumps(event))), contextlib.redirect_stdout(output):
        stop_verify.main()
    return json.loads(output.getvalue())


def invoke_stop_for_turn(root: Path, turn_id: str) -> dict[str, object]:
    from io import StringIO
    import contextlib

    event = {"session_id": "session-1", "turn_id": turn_id, "cwd": str(root)}
    output = StringIO()
    with patch.object(sys, "stdin", StringIO(json.dumps(event))), contextlib.redirect_stdout(output):
        stop_verify.main()
    return json.loads(output.getvalue())


def invoke_start(root: Path, prompt: str) -> None:
    from io import StringIO
    import contextlib

    event = {"session_id": "session-1", "turn_id": "turn-retry", "cwd": str(root), "prompt": prompt}
    with patch.object(sys, "stdin", StringIO(json.dumps(event))), contextlib.redirect_stdout(StringIO()):
        turn_start.main()


def invoke_pretool(root: Path, tool_name: str, turn_id: str = "continuation-turn") -> None:
    from io import StringIO
    import contextlib

    event = {"session_id": "session-1", "turn_id": turn_id, "cwd": str(root), "tool_name": tool_name}
    with patch.object(sys, "stdin", StringIO(json.dumps(event))), contextlib.redirect_stdout(StringIO()):
        turn_start.main()


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = make_repo(self.parent)
        self.state_root = self.parent / "outside-state"
        self.env_patch = patch.dict("os.environ", {"CODEX_HARNESS_STATE_DIR": str(self.state_root)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def start_turn(self) -> Path:
        result = turn_start.repository_snapshot(self.root)
        self.assertIsNotNone(result)
        repo, files = result
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        state = {"session_id": "session-1", "turn_id": "turn-1", "root": repo, "files": files, "failures": 0}
        key = turn_start.state_key("session-1", "turn-1")
        state_path = self.state_root / f"{key}.json"
        state_path.write_text(json.dumps(state))
        session_key = hashlib.sha256(b"session-1").hexdigest()
        (self.state_root / f"{session_key}.active").write_text(state_path.name)
        return state_path

    def active_state_path(self) -> Path:
        session_key = hashlib.sha256(b"session-1").hexdigest()
        active = self.state_root / f"{session_key}.active"
        return self.state_root / active.read_text(encoding="utf-8").strip()

    def test_no_change_and_docs_only_skip_check(self) -> None:
        self.start_turn()
        calls: list[Path] = []
        with patch.object(stop_verify, "run_check", side_effect=lambda path: calls.append(path) or (True, "")), \
                patch.object(stop_verify, "run_risk_gate") as risk_gate:
            self.assertEqual(invoke_stop(self.root), {})
            self.start_turn()
            (self.root / "README.md").write_text("docs changed\n")
            self.assertEqual(invoke_stop(self.root), {})
            self.start_turn()
            (self.root / "NOTES.md").write_text("markdown changed\n")
            self.assertEqual(invoke_stop(self.root), {})
        self.assertEqual(calls, [])
        risk_gate.assert_not_called()

    def test_check_runner_invokes_gradle_wrapper(self) -> None:
        success, reason = stop_verify.run_check(self.root)
        self.assertTrue(success)
        self.assertEqual(reason, "")

    def test_capability_detection_uses_local_contracts(self) -> None:
        self.assertEqual(stop_verify.project_capability(self.root), "BUILD_CONVENTION_PROJECT")
        (self.root / "build.gradle").write_text("plugins { id 'some.other.plugin' }\n")
        self.assertEqual(stop_verify.project_capability(self.root), "GENERIC_PROJECT")
        (self.root / "bin").mkdir()
        (self.root / "bin/check").touch()
        (self.root / "hooks").mkdir()
        (self.root / "hooks/stop_verify.py").touch()
        (self.root / "hooks/turn_start.py").touch()
        (self.root / "config").mkdir()
        (self.root / "config/policy.json").touch()
        self.assertEqual(stop_verify.project_capability(self.root), "HARNESS_PROJECT")

    def test_build_convention_report_missing_is_error_and_risk_not_run(self) -> None:
        self.start_turn()
        (self.root / "Main.java").write_text("class Main { int changed = 1; }\n")
        report = self.root / "build/reports/build-convention/report.json"
        report.unlink()
        with patch.object(stop_verify, "run_check", return_value=(True, "")) as check, \
                patch.object(stop_verify, "run_risk_gate") as risk_gate:
            result = invoke_stop(self.root)
        check.assert_called_once_with(self.root.resolve())
        risk_gate.assert_not_called()
        self.assertIn("NEEDS_REVIEW (ERROR)", result["systemMessage"])
        self.assertIn("Build Convention report is missing", result["systemMessage"])

    def test_harness_project_runs_its_check_without_risk_gate_or_report(self) -> None:
        (self.root / "build.gradle").unlink()
        report = self.root / "build/reports/build-convention/report.json"
        report.unlink()
        (self.root / "bin").mkdir()
        (self.root / "bin/check").touch()
        (self.root / "hooks").mkdir()
        (self.root / "hooks/stop_verify.py").touch()
        (self.root / "hooks/turn_start.py").touch()
        (self.root / "config").mkdir()
        (self.root / "config/policy.json").touch()
        state_path = self.start_turn()
        (self.root / "Main.java").write_text("class Main { int changed = 1; }\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")) as check, \
                patch.object(stop_verify, "run_risk_gate") as risk_gate:
            result = invoke_stop(self.root)
        check.assert_called_once_with(self.root.resolve())
        risk_gate.assert_not_called()
        self.assertEqual(result, {"systemMessage": "Hook · Harness check passed."})
        self.assertFalse(report.exists())
        self.assertFalse(state_path.exists())

    def test_generic_project_skips_unsupported_verification(self) -> None:
        (self.root / "build.gradle").write_text("plugins { id 'some.other.plugin' }\n")
        state_path = self.start_turn()
        (self.root / "Main.java").write_text("class Main { int changed = 1; }\n")
        with patch.object(stop_verify, "run_check") as check, patch.object(stop_verify, "run_risk_gate") as risk_gate:
            result = invoke_stop(self.root)
        check.assert_not_called()
        risk_gate.assert_not_called()
        self.assertIn("verification skipped", result["systemMessage"])
        self.assertFalse(state_path.exists())

    def test_build_convention_success_runs_risk_gate(self) -> None:
        self.start_turn()
        (self.root / "Main.java").write_text("class Main { int changed = 1; }\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")) as check, \
                patch.object(stop_verify, "run_risk_gate", return_value=("PASS", [], "")) as risk_gate:
            result = invoke_stop(self.root)
        check.assert_called_once_with(self.root.resolve())
        risk_gate.assert_called_once_with(self.root.resolve())
        self.assertEqual(result, {"systemMessage": "Gradle check passed. Local Risk Gate: PASS."})

    def test_full_pass_writes_machine_local_attestation(self) -> None:
        self.start_turn()
        (self.root / "build/reports/build-convention/report.json").write_text('{"status":"PASS"}')
        (self.root / "Main.java").write_text("class Main { int value = 1; }\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")), \
                patch.object(stop_verify, "run_risk_gate", return_value=("PASS", [], "", "PASS")):
            invoke_stop(self.root)
        import ship_workflow
        attestation_path = ship_workflow.attestation_path(self.root)
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        self.assertNotEqual(attestation_path.parent, self.root)
        self.assertEqual(attestation["validation"], "PASS")
        self.assertEqual(attestation["riskGate"], "PASS")
        self.assertEqual(attestation["jev"], "PASS")
        self.assertEqual(attestation["buildConvention"], "PASS")
        self.assertEqual(attestation["head"], git_output(self.root, "rev-parse", "HEAD"))

    def test_java_change_runs_check_and_passes_risk_interface(self) -> None:
        self.start_turn()
        (self.root / "Main.java").write_text("class Main { int value = 1; }\n")
        calls: list[Path] = []
        with patch.object(stop_verify, "run_check", side_effect=lambda path: calls.append(path) or (True, "")), \
                patch.object(stop_verify, "run_risk_gate", return_value=("NOT_CONFIGURED", [], "")):
            result = invoke_stop(self.root)
        self.assertEqual(calls, [self.root.resolve()])
        self.assertIn("Gradle check passed", result["systemMessage"])
        self.assertIn("NOT_CONFIGURED", result["systemMessage"])

    def test_preexisting_dirty_file_ignored_unless_modified_this_turn(self) -> None:
        (self.root / "Main.java").write_text("class Main { int preexisting = 1; }\n")
        self.start_turn()
        calls: list[Path] = []
        (self.root / "Main.java").write_text("class Main { int preexisting = 1; int turn = 2; }\n")
        with patch.object(stop_verify, "run_check", side_effect=lambda path: calls.append(path) or (True, "")), \
                patch.object(stop_verify, "run_risk_gate", return_value=("PASS", [], "")):
            self.assertIn("systemMessage", invoke_stop(self.root))
        self.assertEqual(calls, [self.root.resolve()])

    def test_build_gradle_comment_change_is_verify_even_if_already_dirty(self) -> None:
        for preexisting_dirty in (False, True):
            with self.subTest(preexisting_dirty=preexisting_dirty):
                if preexisting_dirty:
                    (self.root / "build.gradle").write_text(
                        "plugins { id 'io.github.dochiri0916.build-convention' }\n// preexisting\n"
                    )
                self.start_turn()
                with (self.root / "build.gradle").open("a", encoding="utf-8") as stream:
                    stream.write("// turn change\n")
                files = stop_verify.changed_files(
                    json.loads(self.active_state_path().read_text())["files"],
                    stop_verify.repository_snapshot(self.root),
                )
                self.assertIn("build.gradle", files)
                self.assertFalse(stop_verify.docs_only(files))

    def test_docs_and_build_gradle_mixed_change_is_verify(self) -> None:
        self.start_turn()
        (self.root / "README.md").write_text("docs changed\n")
        with (self.root / "build.gradle").open("a", encoding="utf-8") as stream:
            stream.write("// verify change\n")
        files = stop_verify.changed_files(
            json.loads(self.active_state_path().read_text())["files"],
            stop_verify.repository_snapshot(self.root),
        )
        self.assertIn("README.md", files)
        self.assertIn("build.gradle", files)
        self.assertFalse(stop_verify.docs_only(files))

    def test_gradle_failure_does_not_run_risk_gate(self) -> None:
        self.start_turn()
        (self.root / "Main.java").write_text("class Main { broken }\n")
        with patch.object(stop_verify, "run_check", return_value=(False, "compile error")), \
                patch.object(stop_verify, "run_risk_gate") as risk_gate:
            result = invoke_stop(self.root)
        self.assertEqual(result["decision"], "block")
        risk_gate.assert_not_called()

    def test_risk_pass_allows_normal_completion(self) -> None:
        self.start_turn()
        (self.root / "Main.java").write_text("class Main { int value = 1; }\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")), \
                patch.object(stop_verify, "run_risk_gate", return_value=("PASS", [], "")):
            result = invoke_stop(self.root)
        self.assertEqual(result, {"systemMessage": "Gradle check passed. Local Risk Gate: PASS."})

    def test_risk_block_continues_with_reason_codes_and_shared_retry_limit(self) -> None:
        state_path = self.start_turn()
        (self.root / "Main.java").write_text("class Main { int value = 1; }\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")), \
                patch.object(stop_verify, "run_risk_gate", return_value=("BLOCK", ["SECRET_WRITE"], "")):
            first = invoke_stop(self.root)
            self.assertEqual(first["decision"], "block")
            self.assertIn("SECRET_WRITE", first["reason"])
            invoke_start(self.root, first["reason"])
            for _ in range(3):
                again = invoke_stop(self.root)
                self.assertEqual(again["decision"], "block")
                invoke_start(self.root, again["reason"])
            fifth = invoke_stop(self.root)
        self.assertIn("NEEDS_REVIEW", fifth["systemMessage"])
        self.assertNotIn("decision", fifth)
        self.assertFalse(state_path.exists())

    def test_risk_review_and_error_need_review_without_continuation(self) -> None:
        for decision, expected in (("REVIEW", "REVIEW"), ("ERROR", "ERROR")):
            with self.subTest(decision=decision):
                state_path = self.start_turn()
                (self.root / "Main.java").write_text(f"class Main {{ int {decision.lower()} = 1; }}\n")
                with patch.object(stop_verify, "run_check", return_value=(True, "")), \
                        patch.object(stop_verify, "run_risk_gate", return_value=(decision, [], "tool issue")):
                    result = invoke_stop(self.root)
                self.assertIn("NEEDS_REVIEW", result["systemMessage"])
                self.assertIn(expected, result["systemMessage"])
                self.assertNotIn("decision", result)
                self.assertFalse(state_path.exists())

    def test_build_gradle_review_has_required_message_and_no_continuation(self) -> None:
        self.start_turn()
        with (self.root / "build.gradle").open("a", encoding="utf-8") as stream:
            stream.write("// harmless comment\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")) as check, \
                patch.object(stop_verify, "run_risk_gate", return_value=("REVIEW", ["RISK_SCORE_REVIEW_THRESHOLD"], "")) as risk_gate:
            result = invoke_stop(self.root)
        check.assert_called_once_with(self.root.resolve())
        risk_gate.assert_called_once_with(self.root.resolve())
        self.assertEqual(result, {"systemMessage": "Hook · Gradle check passed.\nLocal Risk Gate: NEEDS_REVIEW (REVIEW)."})
        self.assertNotIn("decision", result)

    def test_general_question_without_snapshot_is_normal_completion(self) -> None:
        start_turn = {"session_id": "session-1", "turn_id": "turn-a", "cwd": str(self.root)}
        from io import StringIO
        import contextlib
        with patch.object(sys, "stdin", StringIO(json.dumps(start_turn))), contextlib.redirect_stdout(StringIO()):
            turn_start.main()
        path_a = self.state_root / f"{turn_start.state_key('session-1', 'turn-a')}.json"
        self.assertTrue(path_a.is_file())
        (self.root / "README.md").write_text("docs this turn\n")
        stop_event = {"session_id": "session-1", "turn_id": "turn-b", "cwd": str(self.root)}
        output = StringIO()
        with patch.object(sys, "stdin", StringIO(json.dumps(stop_event))), contextlib.redirect_stdout(output):
            stop_verify.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result, {})
        self.assertTrue(path_a.is_file())

    def test_pretool_snapshots_continuation_before_apply_patch_and_detects_change(self) -> None:
        invoke_pretool(self.root, "apply_patch")
        state_path = self.state_root / f"{turn_start.state_key('session-1', 'continuation-turn')}.json"
        self.assertTrue(state_path.is_file())
        (self.root / "Main.java").write_text("class Main { int changed = 1; }\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")) as check, \
                patch.object(stop_verify, "run_risk_gate", return_value=("PASS", [], "")):
            result = invoke_stop_for_turn(self.root, "continuation-turn")
        check.assert_called_once_with(self.root.resolve())
        self.assertEqual(result, {"systemMessage": "Gradle check passed. Local Risk Gate: PASS."})

    def test_pretool_snapshots_before_bash_file_change(self) -> None:
        invoke_pretool(self.root, "Bash", "bash-turn")
        (self.root / "Main.java").write_text("class Main { int changed = 1; }\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")) as check, \
                patch.object(stop_verify, "run_risk_gate", return_value=("PASS", [], "")):
            result = invoke_stop_for_turn(self.root, "bash-turn")
        check.assert_called_once_with(self.root.resolve())
        self.assertEqual(result["systemMessage"], "Gradle check passed. Local Risk Gate: PASS.")

    def test_failed_pretool_snapshot_is_needs_review(self) -> None:
        import contextlib
        from io import StringIO

        with patch.object(turn_start, "repository_snapshot", return_value=None):
            invoke_pretool(self.root, "Write", "failed-snapshot-turn")
        output = StringIO()
        event = {"session_id": "session-1", "turn_id": "failed-snapshot-turn", "cwd": str(self.root)}
        with patch.object(sys, "stdin", StringIO(json.dumps(event))), contextlib.redirect_stdout(output):
            stop_verify.main()
        self.assertIn("NEEDS_REVIEW", json.loads(output.getvalue())["systemMessage"])

    def test_stop_hook_uses_machine_config_without_risk_gate_environment(self) -> None:
        self.start_turn()
        (self.root / "Main.java").write_text("class Main { int value = 1; }\n")
        home = self.parent / "machine-home"
        config = home / ".config" / "codex-harness" / "config.json"
        config.parent.mkdir(parents=True)
        risk_home = self.parent / "configured-risk-gate"
        risk_home.mkdir()
        wrapper = risk_home / "gradlew"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        wrapper.chmod(0o755)
        libs = risk_home / "build" / "libs"
        libs.mkdir(parents=True)
        (libs / "risk-gate.jar").touch()
        fake_bin = self.parent / "fake-bin"
        fake_bin.mkdir()
        fake_java = fake_bin / "java"
        fake_java.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"decision":"PASS","reasonCodes":["CONFIGURED_HOME"]}\'\nexit 0\n')
        fake_java.chmod(0o755)
        config.write_text(json.dumps({"riskGateHome": str(risk_home)}))
        report = self.root / "build/reports/build-convention/report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}")
        with patch.dict(os.environ, {"HOME": str(home), "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}, clear=False), \
                patch.object(stop_verify, "run_check", return_value=(True, "")):
            os.environ.pop("RISK_GATE_HOME", None)
            result = invoke_stop(self.root)
        self.assertEqual(result, {"systemMessage": "Gradle check passed. Local Risk Gate: PASS."})

    def test_jev_keychain_lookup_is_private_and_passes_configured_environment(self) -> None:
        home = self.parent / "machine-home-jev"
        config = home / ".config" / "codex-harness" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"jev": {"enabled": True, "keychainService": "fixture-service"}}))
        child_environment: dict[str, str] = {}
        keychain_result = subprocess.CompletedProcess(
            args=["security"], returncode=0, stdout="fixture-only-key-material\n", stderr=""
        )
        with patch.dict(os.environ, {"HOME": str(home)}, clear=False), \
                patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}, clear=False), \
                patch.object(risk_gate_config.sys, "platform", "darwin"), \
                patch.object(risk_gate_config.subprocess, "run", return_value=keychain_result) as keychain:
            risk_gate_config.configure_jev_environment(child_environment)
        self.assertEqual(child_environment["TYPESAFE_JEV_ENABLED"], "true")
        self.assertEqual(child_environment["TYPESAFE_API_KEY"], "fixture-only-key-material")
        self.assertEqual(keychain.call_args.args[0][0:2], ["security", "find-generic-password"])

    def test_jev_key_from_environment_wins_and_disabled_config_removes_it(self) -> None:
        home = self.parent / "machine-home-jev-precedence"
        config = home / ".config" / "codex-harness" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"jev": {"enabled": True, "keychainService": "fixture-service"}}))
        child_environment = {"TYPESAFE_API_KEY": "environment-fixture-key"}
        with patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            with patch.object(risk_gate_config.subprocess, "run") as keychain:
                risk_gate_config.configure_jev_environment(child_environment)
                keychain.assert_not_called()
            self.assertEqual(child_environment["TYPESAFE_API_KEY"], "environment-fixture-key")
            config.write_text(json.dumps({"jev": {"enabled": False}}))
            risk_gate_config.configure_jev_environment(child_environment)
        self.assertNotIn("TYPESAFE_API_KEY", child_environment)
        self.assertEqual(child_environment["TYPESAFE_JEV_ENABLED"], "false")

    def test_jev_enabled_without_environment_or_keychain_returns_configuration_error(self) -> None:
        home = self.parent / "machine-home-jev-missing"
        config = home / ".config" / "codex-harness" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"jev": {"enabled": True, "keychainService": "fixture-service"}}))
        with patch.dict(os.environ, {"HOME": str(home)}, clear=False), \
                patch.object(risk_gate_config.sys, "platform", "linux"):
            with self.assertRaisesRegex(risk_gate_config.ConfigurationError, "API key is unavailable"):
                risk_gate_config.configure_jev_environment({})

    def test_risk_check_passes_jev_key_only_to_child_and_never_outputs_it(self) -> None:
        home = self.parent / "machine-home-risk-child"
        config_dir = home / ".config" / "codex-harness"
        config_dir.mkdir(parents=True)
        risk_home = self.parent / "risk-child"
        risk_home.mkdir()
        wrapper = risk_home / "gradlew"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        wrapper.chmod(0o755)
        libs = risk_home / "build" / "libs"
        libs.mkdir(parents=True)
        (libs / "risk-gate.jar").touch()
        fake_bin = self.parent / "fake-bin-risk-child"
        fake_bin.mkdir()
        fake_java = fake_bin / "java"
        fake_java.write_text(
            "#!/bin/sh\n"
            "if [ \"$TYPESAFE_API_KEY\" = \"fixture-child-only-key\" ] && [ \"$TYPESAFE_JEV_ENABLED\" = true ]; then\n"
            "  printf '%s\\n' '{\"decision\":\"PASS\",\"reasonCodes\":[\"KEY_FORWARDED\"]}'\n"
            "else\n"
            "  printf '%s\\n' '{\"decision\":\"ERROR\",\"reasonCodes\":[]}'\n"
            "fi\n"
            "exit 0\n"
        )
        fake_java.chmod(0o755)
        (config_dir / "config.json").write_text(json.dumps({
            "riskGateHome": str(risk_home),
            "jev": {"enabled": True, "keychainService": "fixture-service"},
        }))
        report = self.root / "build/reports/build-convention/report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}")
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home),
            "PATH": str(fake_bin) + os.pathsep + environment.get("PATH", ""),
            "TYPESAFE_API_KEY": "fixture-child-only-key",
        })
        result = subprocess.run(
            [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["reasonCodes"], ["KEY_FORWARDED"])
        self.assertNotIn("fixture-child-only-key", result.stdout + result.stderr)

    def test_risk_check_reports_missing_configuration_and_preserves_cli_codes(self) -> None:
        env = os.environ.copy()
        env.pop("RISK_GATE_HOME", None)
        env["HOME"] = str(self.parent / "empty-home")
        missing = subprocess.run(
            [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=env,
            text=True, stdout=subprocess.PIPE, check=False,
        )
        self.assertEqual(missing.returncode, 4)
        self.assertEqual(json.loads(missing.stdout)["decision"], "NOT_CONFIGURED")

        report = self.root / "build/reports/build-convention/report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}")
        risk_home = self.parent / "risk gate"
        risk_home.mkdir()
        fake_cli = risk_home / "gradlew"
        fake_cli.write_text("#!/bin/sh\nexit 0\n")
        fake_cli.chmod(0o755)
        libs = risk_home / "build" / "libs"
        libs.mkdir(parents=True)
        (libs / "risk-gate.jar").touch()
        fake_bin = self.parent / "fake-bin"
        fake_bin.mkdir()
        fake_java = fake_bin / "java"
        fake_java.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_RISK_OUTPUT"\nexit "$FAKE_RISK_EXIT"\n')
        fake_java.chmod(0o755)

        config_dir = Path(env["HOME"]) / ".config" / "codex-harness"
        config_dir.mkdir(parents=True)
        config = config_dir / "config.json"
        config.write_text(json.dumps({"riskGateHome": str(risk_home)}))
        env.pop("RISK_GATE_HOME", None)
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["FAKE_RISK_OUTPUT"] = json.dumps({"decision": "PASS", "reasonCodes": ["CONFIG"]})
        env["FAKE_RISK_EXIT"] = "0"
        configured = subprocess.run(
            [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=env,
            text=True, stdout=subprocess.PIPE, check=False,
        )
        self.assertEqual(configured.returncode, 0)
        self.assertEqual(json.loads(configured.stdout)["reasonCodes"], ["CONFIG"])
        env["RISK_GATE_HOME"] = "/invalid/environment/home"
        invalid_environment = subprocess.run(
            [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=env,
            text=True, stdout=subprocess.PIPE, check=False,
        )
        self.assertEqual(invalid_environment.returncode, 4)
        self.assertEqual(json.loads(invalid_environment.stdout)["decision"], "ERROR")

        env.pop("RISK_GATE_HOME")
        config.write_text("{")
        invalid_json = subprocess.run(
            [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=env,
            text=True, stdout=subprocess.PIPE, check=False,
        )
        self.assertEqual(invalid_json.returncode, 4)
        self.assertEqual(json.loads(invalid_json.stdout)["decision"], "ERROR")

        config.write_text(json.dumps({"riskGateHome": str(self.parent / "missing-risk-gate")}))
        invalid_configured_path = subprocess.run(
            [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=env,
            text=True, stdout=subprocess.PIPE, check=False,
        )
        self.assertEqual(invalid_configured_path.returncode, 4)
        self.assertEqual(json.loads(invalid_configured_path.stdout)["decision"], "ERROR")

        config.write_text(json.dumps({"riskGateHome": str(risk_home)}))
        env_precedence_home = self.parent / "env-risk-gate"
        env_precedence_home.mkdir()
        env_wrapper = env_precedence_home / "gradlew"
        env_wrapper.write_text("#!/bin/sh\nexit 0\n")
        env_wrapper.chmod(0o755)
        env_libs = env_precedence_home / "build" / "libs"
        env_libs.mkdir(parents=True)
        (env_libs / "risk-gate.jar").touch()
        env["RISK_GATE_HOME"] = str(env_precedence_home)
        env["FAKE_RISK_OUTPUT"] = json.dumps({"decision": "REVIEW", "reasonCodes": []})
        env["FAKE_RISK_EXIT"] = "2"
        precedence = subprocess.run(
            [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=env,
            text=True, stdout=subprocess.PIPE, check=False,
        )
        self.assertEqual(precedence.returncode, 2)
        self.assertEqual(json.loads(precedence.stdout)["decision"], "REVIEW")
        env["RISK_GATE_HOME"] = str(risk_home)
        cases = (("PASS", 0), ("REVIEW", 2), ("BLOCK", 3), ("ERROR", 4))
        for decision, code in cases:
            with self.subTest(decision=decision):
                env["FAKE_RISK_OUTPUT"] = json.dumps({"decision": decision, "reasonCodes": ["TEST_CODE"]})
                env["FAKE_RISK_EXIT"] = str(code)
                result = subprocess.run(
                    [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=env,
                    text=True, stdout=subprocess.PIPE, check=False,
                )
                self.assertEqual(result.returncode, code)
                self.assertEqual(json.loads(result.stdout)["decision"], decision)

        for payload, code in (("BLOCK", 1), ("PASS", 3), ("BLOCK", 0)):
            with self.subTest(payload=payload, code=code):
                env["FAKE_RISK_OUTPUT"] = json.dumps({"decision": payload, "reasonCodes": []})
                env["FAKE_RISK_EXIT"] = str(code)
                result = subprocess.run(
                    [str(ROOT / "bin" / "risk-check")], cwd=self.root, env=env,
                    text=True, stdout=subprocess.PIPE, check=False,
                )
                self.assertEqual(result.returncode, 4)
                self.assertEqual(json.loads(result.stdout)["decision"], "ERROR")
                if code == 1:
                    self.assertIn("execution failed (exit 1)", json.loads(result.stdout)["message"])

    def test_failed_check_blocks_then_fifth_failure_needs_review(self) -> None:
        state_path = self.start_turn()
        (self.root / "Main.java").write_text("class Main { broken }\n")
        with patch.object(stop_verify, "run_check", return_value=(False, "compile error")):
            first = invoke_stop(self.root)
            self.assertEqual(first["decision"], "block")
            invoke_start(self.root, first["reason"])
            self.assertEqual(json.loads(self.active_state_path().read_text())["failures"], 1)
            for _ in range(3):
                again = invoke_stop(self.root)
                self.assertEqual(again["decision"], "block")
                invoke_start(self.root, again["reason"])
            fifth = invoke_stop(self.root)
        self.assertIn("NEEDS_REVIEW", fifth["systemMessage"])
        self.assertNotIn("decision", fifth)
        self.assertFalse(state_path.exists())

    def test_untracked_source_is_detected_and_state_is_outside_repository(self) -> None:
        self.start_turn()
        (self.root / "New.kt").write_text("class New\n")
        with patch.object(stop_verify, "run_check", return_value=(True, "")), \
                patch.object(stop_verify, "run_risk_gate", return_value="Local Risk Gate: NOT_CONFIGURED."):
            self.assertIn("systemMessage", invoke_stop(self.root))
        self.assertNotEqual(self.state_root, self.root)
        self.assertFalse((self.root / ".codex-harness").exists())


class ShipWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = make_repo(self.parent)
        git(self.root, "branch", "-m", "base")
        git(self.root, "branch", "main", "HEAD")
        git(self.root, "checkout", "-qB", "feat/ship")
        git(self.root, "remote", "add", "origin", "git@github.com:owner/repo.git")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.state_root = self.parent / "outside-state"
        self.home = self.parent / "home"
        config = self.home / ".config/codex-harness/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"ship": {"enabled": True, "remote": "origin"}}))
        self.env_patch = patch.dict(os.environ, {
            "CODEX_HARNESS_STATE_DIR": str(self.state_root), "HOME": str(self.home),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.calls: list[list[str]] = []
        self.existing_pr = False
        self.default = "main"
        self.real_run = subprocess.run
        self.run_patch = patch.object(ship_workflow.subprocess, "run", side_effect=self.fake_run)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def fake_run(self, args: list[str], *positional: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        args = [str(arg) for arg in args]
        if args[0] == "git" and "push" in args:
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "gh":
            self.calls.append(args)
            if args[1:3] == ["repo", "view"]:
                return subprocess.CompletedProcess(args, 0, self.default + "\n", "")
            if args[1:3] == ["pr", "list"]:
                body = '[{"number":7}]' if self.existing_pr else "[]"
                return subprocess.CompletedProcess(args, 0, body, "")
            if args[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(args, 0, "https://github.com/owner/repo/pull/8\n", "")
            if args[1:3] == ["pr", "view"]:
                return subprocess.CompletedProcess(args, 0, "8\n", "")
        return self.real_run(args, *positional, **kwargs)  # type: ignore[return-value]

    def attest(self, **overrides: object) -> None:
        (self.root / "Main.java").write_text("class Main { int shipped = 1; }\n")
        values: dict[str, object] = {
            "build_status": "PASS", "risk_status": "PASS", "jev_status": "PASS",
            "session_clean_start": True,
        }
        values.update(overrides)
        ship_workflow.create_attestation(self.root, **values)  # type: ignore[arg-type]

    def assert_no_publication_mutations(self) -> None:
        self.assertFalse(any("push" in call or call[1:3] == ["pr", "create"] for call in self.calls))

    def test_pass_attestation_commits_pushes_and_creates_pr(self) -> None:
        self.attest()
        result = ship_workflow.ship(self.root, "feat: ship safely", "Ship safely")
        self.assertEqual(result["status"], "SHIPPED")
        self.assertEqual(result["prNumber"], 8)
        self.assertEqual(self.real_run(["git", "-C", str(self.root), "log", "-1", "--pretty=%s"], check=True, capture_output=True, text=True).stdout.strip(), "feat: ship safely")
        push = next(call for call in self.calls if "push" in call)
        self.assertIn("-u", push)
        self.assertFalse(any(arg.startswith("--force") for arg in push))
        create = next(call for call in self.calls if call[1:3] == ["pr", "create"])
        self.assertIn("--base", create)
        self.assertIn("main", create)

    def test_existing_pr_is_reused_without_create(self) -> None:
        self.existing_pr = True
        self.attest()
        result = ship_workflow.ship(self.root)
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["prNumber"], 7)
        self.assertFalse(any(call[1:3] == ["pr", "create"] for call in self.calls))

    def test_changed_tree_is_stale_and_not_committed(self) -> None:
        self.attest()
        (self.root / "Main.java").write_text("changed after validation\n")
        with self.assertRaisesRegex(ship_workflow.ShipError, "stale") as error:
            ship_workflow.ship(self.root)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.real_run(["git", "-C", str(self.root), "log", "-1", "--pretty=%s"], check=True, capture_output=True, text=True).stdout.strip(), "initial")
        self.assert_no_publication_mutations()

    def test_default_branch_is_rejected_before_mutation(self) -> None:
        git(self.root, "branch", "-f", "release", "HEAD")
        self.default = "release"
        git(self.root, "checkout", "-q", "release")
        self.attest()
        with self.assertRaisesRegex(ship_workflow.ShipError, "default branch"):
            ship_workflow.ship(self.root)
        self.assert_no_publication_mutations()

    def test_main_branch_is_rejected_before_mutation(self) -> None:
        git(self.root, "checkout", "-q", "main")
        self.attest()
        with self.assertRaisesRegex(ship_workflow.ShipError, "main or default"):
            ship_workflow.ship(self.root)
        self.assert_no_publication_mutations()

    def test_risk_decisions_and_incomplete_jev_are_rejected(self) -> None:
        for risk, jev, code in (("REVIEW", "PASS", 2), ("BLOCK", "PASS", 3),
                                ("ERROR", "PASS", 4), ("PASS", "NOT_RUN", 2)):
            with self.subTest(risk=risk, jev=jev):
                (self.root / "Main.java").write_text(f"{risk} {jev}\n")
                ship_workflow.create_attestation(self.root, "PASS", risk, jev, session_clean_start=True)
                with self.assertRaises(ship_workflow.ShipError) as error:
                    ship_workflow.ship(self.root)
                self.assertEqual(error.exception.code, code)
        self.assert_no_publication_mutations()

    def test_dirty_session_start_is_rejected(self) -> None:
        self.attest(session_clean_start=False)
        with self.assertRaisesRegex(ship_workflow.ShipError, "clean when"):
            ship_workflow.ship(self.root)
        self.assert_no_publication_mutations()


if __name__ == "__main__":
    unittest.main()
