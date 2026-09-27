"""Attestation and publication workflow used by the explicit ship command."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from hooks import stop_verify
import risk_gate_config


class ShipError(Exception):
    def __init__(self, message: str, code: int = 4):
        super().__init__(message)
        self.code = code


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, check=False,
    )
    if check and result.returncode:
        raise ShipError("Git command failed.")
    return result.stdout.strip()


def repository_identity(root: Path) -> str:
    remote = git(root, "remote", "get-url", "origin", check=False)
    return hashlib.sha256(f"{root.resolve()}\n{remote}".encode()).hexdigest()


def fingerprint(root: Path) -> str:
    payload = json.dumps(stop_verify.repository_snapshot(root), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def attestation_path(root: Path) -> Path:
    return stop_verify.state_dir() / f"validation-{repository_identity(root)}.json"


def create_attestation(
    root: Path, build_status: str, risk_status: str, jev_status: str,
    *, session_clean_start: bool,
) -> None:
    branch = git(root, "branch", "--show-current")
    head = git(root, "rev-parse", "HEAD")
    payload = {
        "repositoryIdentity": repository_identity(root),
        "branch": branch,
        "head": head,
        "workingTreeFingerprint": fingerprint(root),
        "validation": "PASS",
        "verifier": stop_verify.verifier_label(stop_verify.project_capability(root)),
        "buildConvention": build_status,
        "riskGate": risk_status,
        "jev": jev_status,
        "validationTimestamp": datetime.now(timezone.utc).isoformat(),
        "sessionStartedClean": session_clean_start,
    }
    path = attestation_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
        stream.write("\n")


def load_config() -> dict[str, object]:
    try:
        config = risk_gate_config.load_machine_config()
    except risk_gate_config.ConfigurationError as exc:
        raise ShipError("Machine config is invalid.") from exc
    ship = config.get("ship", {})
    if not isinstance(ship, dict):
        raise ShipError("ship config must be an object.")
    enabled = ship.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ShipError("ship.enabled must be a boolean.")
    if not enabled:
        raise ShipError("Automatic ship is disabled in machine config.")
    remote = ship.get("remote", "origin")
    if not isinstance(remote, str) or not remote.strip() or remote != "origin":
        raise ShipError("ship.remote must be origin.")
    return config


def default_branch(root: Path, repo: str) -> str:
    result = subprocess.run(
        ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise ShipError("Could not determine the remote default branch.")
    return result.stdout.strip()


def require_github_remote(root: Path, remote: str) -> tuple[str, str]:
    url = git(root, "remote", "get-url", remote, check=False)
    if not url or "github.com" not in url.lower():
        raise ShipError("A GitHub origin remote is required.")
    repo = url.removeprefix("git@github.com:").removeprefix("ssh://git@github.com/")
    repo = repo.removeprefix("https://github.com/").removeprefix("http://github.com/").removesuffix(".git").strip("/")
    if repo.count("/") != 1 or any(ch.isspace() for ch in repo):
        raise ShipError("Could not identify the GitHub repository from origin.")
    return url, repo


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, separators=(",", ":")))


def safe_summary(value: str) -> str:
    value = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b|\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    value = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", value)
    return value.strip()


def ship(root: Path, message: str | None = None, summary: str | None = None) -> dict[str, object]:
    root = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    config = load_config()
    remote = config.get("ship", {}).get("remote", "origin")  # type: ignore[union-attr]
    _, repo = require_github_remote(root, str(remote))
    branch = git(root, "branch", "--show-current")
    if not branch:
        raise ShipError("Detached HEAD cannot be shipped.")
    base = default_branch(root, repo)
    if branch in {"main", "master", base}:
        raise ShipError("Automatic ship is disabled on the main or default branch.")
    attestation_file = attestation_path(root)
    try:
        attestation = json.loads(attestation_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShipError("No successful validation attestation exists.", 2) from exc
    if not isinstance(attestation, dict):
        raise ShipError("Validation attestation is invalid.")
    head = git(root, "rev-parse", "HEAD")
    if (
        attestation.get("repositoryIdentity") != repository_identity(root)
        or attestation.get("branch") != branch
        or attestation.get("head") != head
        or attestation.get("workingTreeFingerprint") != fingerprint(root)
    ):
        raise ShipError("Validation attestation is stale; rerun Stop validation.", 2)
    if attestation.get("sessionStartedClean") is not True:
        raise ShipError("Automatic ship requires a repository that was clean when the Codex session started.", 4)
    if attestation.get("validation") != "PASS" or attestation.get("buildConvention") != "PASS":
        raise ShipError("Validation requires review; ship is blocked.", 2)
    if attestation.get("riskGate") in {"REVIEW", "NEEDS_REVIEW"}:
        raise ShipError("Local Risk Gate requires review; ship is blocked.", 2)
    if attestation.get("riskGate") == "ERROR":
        raise ShipError("Local Risk Gate encountered an execution error.", 4)
    if attestation.get("riskGate") == "BLOCK":
        raise ShipError("Local Risk Gate blocked shipping.", 3)
    if attestation.get("riskGate") != "PASS":
        raise ShipError("Local Risk Gate did not complete successfully.", 4)
    if attestation.get("jev") != "PASS":
        raise ShipError("Jev evaluation is incomplete or did not pass.", 2)
    status = git(root, "status", "--porcelain", "--untracked-files=all")
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if upstream and upstream != f"{remote}/{branch}":
        raise ShipError("The feature branch upstream must point to its same-named branch on origin.")
    if status:
        commit_message = message or "feat: complete codex goal"
        if not commit_message.strip() or "\n" in commit_message:
            raise ShipError("Commit message must be a non-empty single line.")
        if fingerprint(root) != attestation.get("workingTreeFingerprint"):
            raise ShipError("Validation attestation is stale; rerun Stop validation.", 2)
        git(root, "add", "-A")
        result = subprocess.run(
            ["git", "-C", str(root), "commit", "-m", commit_message],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        if result.returncode:
            raise ShipError("Git commit failed.")
        commit = git(root, "rev-parse", "HEAD")
        attestation_file.unlink(missing_ok=True)
    else:
        commit = head
    push_args = ["git", "-C", str(root), "push"]
    if not upstream:
        push_args += ["-u", str(remote), branch]
    result = subprocess.run(push_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        raise ShipError("Git push failed.")
    existing = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number", "--limit", "1"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    if existing.returncode:
        raise ShipError("Could not inspect open pull requests.")
    try:
        prs = json.loads(existing.stdout)
    except json.JSONDecodeError as exc:
        raise ShipError("GitHub CLI returned invalid pull request data.") from exc
    if prs:
        number = prs[0].get("number")
        if not isinstance(number, int):
            raise ShipError("GitHub CLI returned an invalid pull request number.")
        return {"status": "UPDATED", "branch": branch, "commit": commit[:12], "prNumber": number}
    title = safe_summary(summary or message or "Complete Codex goal").splitlines()[0][:120]
    if not title:
        title = "Complete Codex goal"
    body = "\n".join([
        "## Summary", "", f"- {title}", "", "## Validation", "",
        "- Verifier: PASS", "- Build Convention: PASS", "- Local Risk Gate: PASS", "- Jev: PASS",
    ])
    created = subprocess.run(
        ["gh", "pr", "create", "--repo", repo, "--base", base, "--head", branch,
         "--title", title, "--body", body], cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    if created.returncode:
        raise ShipError("Pull request creation failed.")
    viewed = subprocess.run(
        ["gh", "pr", "view", "--repo", repo, "--json", "number", "--jq", ".number"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    try:
        number = int(viewed.stdout.strip())
    except ValueError as exc:
        raise ShipError("Created pull request number could not be read.") from exc
    return {"status": "SHIPPED", "branch": branch, "commit": commit[:12], "prNumber": number}
