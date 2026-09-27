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
    def __init__(self, message: str, code: int = 4, *, category: str | None = None):
        super().__init__(message)
        self.code = code
        self.category = category


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, check=False,
    )
    if check and result.returncode:
        raise ShipError("Git command failed.")
    return result.stdout.strip()


def github(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run every GitHub CLI command in the target repository context."""
    return subprocess.run(
        ["gh", *args], cwd=root, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, check=False,
    )


def repository_identity(root: Path) -> str:
    remote = git(root, "remote", "get-url", "origin", check=False)
    return hashlib.sha256(f"{root.resolve()}\n{remote}".encode()).hexdigest()


def fingerprint(root: Path) -> str:
    payload = json.dumps(stop_verify.repository_snapshot(root), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def attestation_path(root: Path) -> Path:
    return stop_verify.state_dir() / f"validation-{repository_identity(root)}.json"


def ship_state_path(root: Path) -> Path:
    return stop_verify.state_dir() / f"ship-{repository_identity(root)}.json"


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
    result = github(root, "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name")
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


PR_FIELDS = "number,url,state,headRefName,headRefOid"


def pull_request(root: Path, repo: str, branch: str, head: str) -> dict[str, object]:
    result = github(root, "pr", "view", branch, "--repo", repo, "--json", PR_FIELDS)
    if result.returncode:
        diagnostic = result.stderr.lower()
        if "no pull requests found" in diagnostic or "could not find any pull requests" in diagnostic:
            raise ShipError("No pull request was found for the target branch.", category="PR_NOT_FOUND")
        raise ShipError("Could not read pull request metadata.", category="GH_COMMAND_FAILED")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ShipError("GitHub CLI returned invalid pull request data.", category="MALFORMED_PR_METADATA") from exc
    if not isinstance(data, dict):
        raise ShipError("GitHub CLI returned invalid pull request data.", category="MALFORMED_PR_METADATA")
    number, url = data.get("number"), data.get("url")
    state, head_name, head_oid = data.get("state"), data.get("headRefName"), data.get("headRefOid")
    if (not isinstance(number, int) or isinstance(number, bool) or number <= 0
            or not isinstance(url, str) or not url.strip()
            or not isinstance(state, str) or not state.strip()
            or not isinstance(head_name, str) or not head_name.strip()
            or not isinstance(head_oid, str) or not head_oid.strip()):
        raise ShipError("GitHub CLI returned incomplete pull request metadata.", category="MALFORMED_PR_METADATA")
    if state != "OPEN" or head_name != branch or head_oid != head:
        raise ShipError("Pull request state or head does not match the pushed branch.", category="PR_HEAD_MISMATCH")
    return {"prNumber": number, "prUrl": url}


def remote_head(root: Path, remote: str, branch: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if result.returncode:
        raise ShipError("Could not inspect the remote branch.")
    fields = result.stdout.strip().split()
    if len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
        raise ShipError("Remote branch does not match the recorded ship state.")
    return fields[0]


def write_ship_state(root: Path, branch: str, head: str, attestation: dict[str, object]) -> None:
    path = ship_state_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({
        "repositoryIdentity": repository_identity(root), "branch": branch,
        "pushedHead": head, "validation": "PASS", "attestation": attestation,
    }, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def recover_partial_ship(root: Path, repo: str, remote: str, branch: str, head: str) -> dict[str, object]:
    try:
        state = json.loads(ship_state_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = None
    except json.JSONDecodeError as exc:
        raise ShipError("Partial ship state is invalid.") from exc
    if state is None:
        if git(root, "status", "--porcelain", "--untracked-files=all"):
            raise ShipError("Partial ship recovery requires a clean working tree.")
        if remote_head(root, remote, branch) != head:
            raise ShipError("Remote branch does not match the current HEAD.")
        pr = pull_request(root, repo, branch, head)
        return {"status": "UPDATED", "branch": branch, "commit": head[:12], **pr}
    validation = state.get("attestation") if isinstance(state, dict) else None
    if (not isinstance(state, dict) or state.get("repositoryIdentity") != repository_identity(root)
            or state.get("branch") != branch or state.get("pushedHead") != head
            or state.get("validation") != "PASS" or not isinstance(validation, dict)
            or validation.get("repositoryIdentity") != repository_identity(root)
            or validation.get("branch") != branch or validation.get("validation") != "PASS"
            or validation.get("buildConvention") != "PASS" or validation.get("riskGate") != "PASS"
            or validation.get("jev") != "PASS" or validation.get("sessionStartedClean") is not True):
        raise ShipError("No matching partial ship state exists.", 2)
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ShipError("Partial ship recovery requires a clean working tree.")
    if remote_head(root, remote, branch) != head:
        raise ShipError("Remote branch does not match the recorded ship state.")
    pr = pull_request(root, repo, branch, head)
    ship_state_path(root).unlink(missing_ok=True)
    return {"status": "UPDATED", "branch": branch, "commit": head[:12], **pr}


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
        attestation_text = attestation_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return recover_partial_ship(root, repo, str(remote), branch, git(root, "rev-parse", "HEAD"))
    try:
        attestation = json.loads(attestation_text)
    except json.JSONDecodeError as exc:
        raise ShipError("Validation attestation is invalid.") from exc
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
    write_ship_state(root, branch, commit, attestation)
    existing = github(root, "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number", "--limit", "1")
    if existing.returncode:
        raise ShipError("Could not inspect open pull requests.")
    try:
        prs = json.loads(existing.stdout)
    except json.JSONDecodeError as exc:
        raise ShipError("GitHub CLI returned invalid pull request data.") from exc
    if prs:
        pr = pull_request(root, repo, branch, commit)
        return {"status": "UPDATED", "branch": branch, "commit": commit[:12], **pr}
    title = safe_summary(summary or message or "Complete Codex goal").splitlines()[0][:120]
    if not title:
        title = "Complete Codex goal"
    body = "\n".join([
        "## Summary", "", f"- {title}", "", "## Validation", "",
        "- Verifier: PASS", "- Build Convention: PASS", "- Local Risk Gate: PASS", "- Jev: PASS",
    ])
    created = github(root, "pr", "create", "--repo", repo, "--base", base, "--head", branch,
                     "--title", title, "--body", body)
    if created.returncode:
        raise ShipError("Pull request creation failed.")
    pr = pull_request(root, repo, branch, commit)
    ship_state_path(root).unlink(missing_ok=True)
    return {"status": "SHIPPED", "branch": branch, "commit": commit[:12], **pr}
