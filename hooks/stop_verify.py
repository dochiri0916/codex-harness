#!/usr/bin/env python3
"""Verify files changed since the current Codex prompt began."""

from __future__ import annotations

import hashlib
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HARNESS_ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((HARNESS_ROOT / "config" / "policy.json").read_text(encoding="utf-8"))
MAX_FAILURES = int(POLICY["max_consecutive_failures"])
LOG_LIMIT = int(POLICY["failure_log_limit_chars"])
BUILD_CONVENTION_PLUGIN = "io.github.dochiri0916.build-convention"


def _gradle_configuration(root: Path) -> list[Path]:
    return [root / name for name in (
        "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    ) if (root / name).is_file()]


def _uses_build_convention(path: Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    source = re.sub(r"(?s)/\*.*?\*/", "", source)
    source = re.sub(r"(?m)//.*$", "", source)
    plugin = re.escape(BUILD_CONVENTION_PLUGIN)
    patterns = (
        rf"\bid\s*\(?\s*['\"]{plugin}['\"]",
        rf"\bapply\s*\(?\s*plugin\s*[:=]\s*['\"]{plugin}['\"]",
    )
    return any(re.search(pattern, source) for pattern in patterns)


def project_capability(root: Path) -> str:
    """Classify projects from repository-local verifier and Gradle contracts."""
    if any(_uses_build_convention(path) for path in _gradle_configuration(root)):
        return "BUILD_CONVENTION_PROJECT"
    if (
        (root / "bin" / "check").is_file()
        and (root / "hooks" / "stop_verify.py").is_file()
        and (root / "hooks" / "turn_start.py").is_file()
        and (root / "config" / "policy.json").is_file()
    ):
        return "HARNESS_PROJECT"
    return "GENERIC_PROJECT"


def verifier_label(capability: str) -> str:
    return "Harness check" if capability == "HARNESS_PROJECT" else "Gradle check"


def state_dir() -> Path:
    configured = os.environ.get("CODEX_HARNESS_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "codex-harness"


def state_key(session_id: str, turn_id: str) -> str:
    session_key = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()
    turn_key = hashlib.sha256(turn_id.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{session_key}-{turn_key}"


def clear_state(path: Path, session_id: str) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    session_key = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()
    active_path = state_dir() / f"{session_key}.active"
    try:
        if active_path.read_text(encoding="utf-8").strip() == path.name:
            active_path.unlink()
    except FileNotFoundError:
        pass


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    ).stdout


def digest(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return "symlink:" + hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        executable = bool(path.stat().st_mode & 0o111)
        return ("exec:" if executable else "file:") + h.hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def repository_snapshot(root: Path) -> dict[str, dict[str, object]]:
    tracked = set(git(root, "ls-files", "-z").split(b"\0")) - {b""}
    try:
        tracked.update(set(git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD").split(b"\0")) - {b""})
    except subprocess.CalledProcessError:
        pass
    untracked = set(git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")) - {b""}
    index_entries: dict[bytes, list[str]] = {}
    for item in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if item:
            metadata, raw_path = item.split(b"\t", 1)
            index_entries.setdefault(raw_path, []).append(metadata.decode("ascii", "replace"))
    current = {}
    for raw_path in tracked | untracked:
        name = os.fsdecode(raw_path)
        current[name] = {
            "tracked": raw_path in tracked,
            "index": sorted(index_entries.get(raw_path, [])),
            "worktree": digest(root / name),
        }
    return current


def changed_files(before: dict[str, dict[str, object]], after: dict[str, dict[str, object]]) -> list[str]:
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def docs_only(paths: list[str]) -> bool:
    def matches(name: str, pattern: str) -> bool:
        if fnmatch.fnmatchcase(name, pattern):
            return True
        return pattern.startswith("**/") and fnmatch.fnmatchcase(name, pattern[3:])

    for name in paths:
        if any(matches(name, pattern) for pattern in POLICY["docs_only"]):
            continue
        return False
    return bool(paths)


def safe_log(output: bytes) -> str:
    text = output.decode("utf-8", "replace")
    for name, value in os.environ.items():
        if re.search(r"(?i)(password|passwd|token|secret|api[_-]?key|credential|authorization)", name) and len(value) >= 4:
            text = text.replace(value, "[REDACTED_ENV]")
    patterns = [
        (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"), r"\1[REDACTED]"),
        (re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)\b(\s*[=:]\s*)[^\s,;]+"), r"\1\2[REDACTED]"),
        (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"), "[REDACTED_TOKEN]"),
        (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_TOKEN]"),
    ]
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    lines = text.splitlines()
    relevant = [line for line in lines if re.search(
        r"(?i)(FAILURE:|BUILD FAILED|\bfailed\b|\berror\b|exception|\* What went wrong|\* Try:|\* Exception is:)", line
    )]
    selected = relevant[-24:] if relevant else lines[-24:]
    result = "\n".join(selected)[-LOG_LIMIT:].strip()
    return result or "Gradle check failed without a readable diagnostic."


def run_check(root: Path) -> tuple[bool, str]:
    capability = project_capability(root)
    label = verifier_label(capability)
    try:
        result = subprocess.run(
            [str(HARNESS_ROOT / "bin" / "check")], cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=1800, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = f"{label} could not complete ({type(exc).__name__}). Fix the cause and rerun verification."
        return False, reason
    if result.returncode == 0:
        return True, ""
    detail = safe_log(result.stdout)
    failure = f"{label} failed" if (root / "gradlew").exists() or capability == "HARNESS_PROJECT" else "Verification could not run"
    return False, f"{failure} (exit {result.returncode}). Fix the reported failure and rerun verification:\n{detail}"


def run_risk_gate(root: Path) -> tuple[str, list[str], str]:
    command = HARNESS_ROOT / "bin" / "risk-check"
    try:
        result = subprocess.run(
            [str(command)], cwd=root, env=os.environ.copy(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=960, check=False, text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "ERROR", [], f"risk-check could not run ({type(exc).__name__})."
    try:
        payload = json.loads(result.stdout)
        decision = payload.get("decision") if isinstance(payload, dict) else None
        reason_codes = payload.get("reasonCodes", []) if isinstance(payload, dict) else None
        if decision not in ("PASS", "BLOCK", "REVIEW", "NOT_CONFIGURED", "ERROR"):
            raise ValueError("unrecognized result")
        if not isinstance(reason_codes, list) or not all(isinstance(code, str) for code in reason_codes):
            raise ValueError("invalid reason codes")
        if decision == "PASS" and result.returncode != 0:
            raise ValueError("exit code mismatch")
        if decision == "REVIEW" and result.returncode != 2:
            raise ValueError("exit code mismatch")
        if decision == "BLOCK" and result.returncode != 3:
            raise ValueError("exit code mismatch")
        if decision in ("NOT_CONFIGURED", "ERROR") and result.returncode != 4:
            raise ValueError("exit code mismatch")
        message = payload.get("message", "")
        return decision, reason_codes, message if isinstance(message, str) else ""
    except (json.JSONDecodeError, ValueError, AttributeError):
        return "ERROR", [], "risk-check returned invalid output."


def write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def emit(payload: dict[str, str] | dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True))


def main() -> int:
    try:
        event = json.load(sys.stdin)
        session_id = event.get("session_id")
        turn_id = event.get("turn_id")
        cwd = event.get("cwd")
        if not isinstance(session_id, str) or not session_id or not isinstance(turn_id, str) or not turn_id or not isinstance(cwd, str):
            emit({})
            return 0
        state_path = state_dir() / f"{state_key(session_id, turn_id)}.json"
        if not state_path.is_file():
            emit({"systemMessage": "NEEDS_REVIEW: Codex Harness has no snapshot for this turn; verification could not determine changed files."})
            return 0
        state = json.loads(state_path.read_text(encoding="utf-8"))
        root = Path(state["root"])
        if Path(cwd).resolve() != root:
            emit({})
            return 0
        current = repository_snapshot(root)
        files = changed_files(state.get("files", {}), current)
        if not files:
            clear_state(state_path, session_id)
            emit({})
            return 0
        if docs_only(files):
            clear_state(state_path, session_id)
            emit({})
            return 0

        capability = project_capability(root)
        if capability == "GENERIC_PROJECT":
            clear_state(state_path, session_id)
            emit({"systemMessage": "Hook · No supported verifier for this project; verification skipped."})
            return 0

        success, reason = run_check(root)
        if success:
            if capability == "HARNESS_PROJECT":
                clear_state(state_path, session_id)
                emit({"systemMessage": "Hook · Harness check passed."})
                return 0
            report = root / "build" / "reports" / "build-convention" / "report.json"
            if not report.is_file():
                clear_state(state_path, session_id)
                emit({"systemMessage": "Gradle check passed. Local Risk Gate: NEEDS_REVIEW (ERROR). Build Convention report is missing."})
                return 0
            decision, reason_codes, detail = run_risk_gate(root)
            if decision == "PASS":
                clear_state(state_path, session_id)
                emit({"systemMessage": "Gradle check passed. Local Risk Gate: PASS."})
            elif decision == "BLOCK":
                failures = int(state.get("failures", 0)) + 1
                state["failures"] = failures
                write_state(state_path, state)
                codes = ", ".join(reason_codes) if reason_codes else "unspecified"
                if failures >= MAX_FAILURES:
                    clear_state(state_path, session_id)
                    emit({"systemMessage": f"NEEDS_REVIEW: Local Risk Gate blocked {failures} times; automatic continuation limit reached. reasonCodes: {codes}."})
                else:
                    marker = f"[[CODEX_HARNESS_RETRY:{hashlib.sha256(session_id.encode('utf-8', 'surrogatepass')).hexdigest()[:12]}]]"
                    emit({"decision": "block", "reason": f"{marker} Local Risk Gate: BLOCK. reasonCodes: {codes}. Fix the reported risks and rerun Gradle check and Risk Gate. Automatic verification attempt {failures} of {MAX_FAILURES}."})
            elif decision == "NOT_CONFIGURED":
                clear_state(state_path, session_id)
                emit({"systemMessage": "Gradle check passed. Local Risk Gate: NOT_CONFIGURED. Configure riskGateHome in ~/.config/codex-harness/config.json or set RISK_GATE_HOME."})
            elif decision == "REVIEW":
                clear_state(state_path, session_id)
                emit({"systemMessage": "Hook · Gradle check passed.\nLocal Risk Gate: NEEDS_REVIEW (REVIEW)."})
            else:
                clear_state(state_path, session_id)
                suffix = f" {detail}" if detail else ""
                emit({"systemMessage": f"Gradle check passed. Local Risk Gate: NEEDS_REVIEW (ERROR).{suffix}"})
            return 0

        failures = int(state.get("failures", 0)) + 1
        state["failures"] = failures
        write_state(state_path, state)
        if failures >= MAX_FAILURES:
            clear_state(state_path, session_id)
            emit({"systemMessage": f"NEEDS_REVIEW: Gradle check failed {failures} times; automatic continuation limit reached. {reason}"})
            return 0
        marker = f"[[CODEX_HARNESS_RETRY:{hashlib.sha256(session_id.encode('utf-8', 'surrogatepass')).hexdigest()[:12]}]]"
        emit({"decision": "block", "reason": f"{marker} {reason}\nAutomatic verification attempt {failures} of {MAX_FAILURES}. Fix the failure and stop again to rerun the check."})
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
        # Hook failures must be visible without exposing paths or environment values.
        emit({"systemMessage": "NEEDS_REVIEW: Codex Harness could not read its snapshot or inspect the repository."})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
