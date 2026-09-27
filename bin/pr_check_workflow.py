"""Single-shot classification of required checks on the current open PR."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import github_adapter
import risk_gate_config
import ship_workflow


EXIT_CODES = {"PASS": 0, "PENDING": 1, "NEEDS_REVIEW": 2, "REPAIRABLE_FAILURE": 3, "ERROR": 4}


class PRCheckError(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, check=False,
    )
    if result.returncode:
        raise PRCheckError("Could not inspect the current Git repository.")
    return result.stdout.strip()


def _remote_head(root: Path, remote: str, branch: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if result.returncode:
        raise PRCheckError("Could not inspect the remote branch.")
    fields = result.stdout.strip().split()
    if len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
        raise PRCheckError("The current branch is missing from the remote.")
    return fields[0]


def _protocol(value: str) -> str | None:
    try:
        import json
        payload = json.loads(value)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        decision = payload.get("decision") or payload.get("status")
        if isinstance(decision, str):
            decision = decision.upper()
            if decision in {"BLOCK", "REVIEW", "ERROR", "PASS"}:
                return decision
    match = re.search(r"\b(?:decision|status)\s*[\"']?\s*[:=]\s*[\"']?\s*(BLOCK|REVIEW|ERROR)\b", value, re.IGNORECASE)
    return match.group(1).upper() if match else None


def _classify_failure(check: dict[str, Any], steps: list[str]) -> tuple[str, str, str]:
    name = check["name"]
    text = " ".join((check.get("summary", ""), check.get("text", ""), *steps))
    combined = f"{name} {text}".lower().replace("-", " ")
    protocol = _protocol(text)
    if any(term in combined for term in ("infrastructure", "runner lost", "timed out waiting for", "service unavailable", "rate limit")):
        return "ERROR", "CI_INFRASTRUCTURE", "CI infrastructure failed."
    is_risk_gate, is_jev = "risk gate" in combined, "jev" in combined
    if protocol == "ERROR":
        return "ERROR", "RISK_GATE" if is_risk_gate else "JEV" if is_jev else "PROVIDER", "Provider reported ERROR."
    if protocol == "REVIEW" and not (is_risk_gate or is_jev):
        return "NEEDS_REVIEW", "AMBIGUOUS", "Provider reported REVIEW."
    if protocol == "ERROR" and (is_risk_gate or is_jev):
        return "ERROR", "RISK_GATE" if is_risk_gate else "JEV", "Provider reported ERROR."
    if protocol == "REVIEW" and (is_risk_gate or is_jev):
        return "NEEDS_REVIEW", "RISK_GATE" if is_risk_gate else "JEV", "Provider requires semantic review."
    if protocol == "BLOCK" and (is_risk_gate or is_jev):
        return "REPAIRABLE_FAILURE", "RISK_GATE" if is_risk_gate else "JEV", "Provider reported a deterministic BLOCK."
    if any(term in combined for term in ("convention", "checkstyle", "pmd", "spotbugs", "architecture")):
        return "REPAIRABLE_FAILURE", "BUILD_CONVENTION", _summary(check)
    if any(term in combined for term in ("compile", "compilation", "test", "tests", "junit")):
        return "REPAIRABLE_FAILURE", "TEST" if any(term in combined for term in ("test", "tests", "junit")) else "COMPILE", _summary(check)
    if "semgrep" in combined or "security scan" in combined:
        return "REPAIRABLE_FAILURE", "SEMGREP", _summary(check)
    if is_risk_gate:
        return "NEEDS_REVIEW", "RISK_GATE", "Risk Gate outcome is ambiguous."
    if is_jev:
        return "NEEDS_REVIEW", "JEV", "Jev outcome is ambiguous."
    if check.get("conclusion") in {"ACTION_REQUIRED", "STALE"}:
        return "NEEDS_REVIEW", "POLICY", "The check requires a manual decision."
    return "NEEDS_REVIEW", "AMBIGUOUS", "Failure could not be classified as a deterministic code issue."


def _summary(check: dict[str, Any]) -> str:
    raw = check.get("summary") or check.get("text") or check["name"]
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:240] or "Check failed."


def _failure_evidence(check: dict[str, Any], category: str, summary: str, steps: list[str]) -> dict[str, object]:
    evidence: dict[str, object] = {
        "check": github_adapter._safe_text(check["name"])[:120],
        "category": category,
        "conclusion": check.get("conclusion") if check.get("conclusion") in {"SUCCESS", "FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE"} else "FAILURE",
        "failedStep": ", ".join(steps) if steps else github_adapter._safe_text(_summary(check))[:240],
        "summary": summary,
    }
    if check.get("runId"):
        evidence["runId"] = check["runId"]
    if check.get("detailsUrl"):
        evidence["url"] = github_adapter._safe_text(check["detailsUrl"])[:500]
    return evidence


def inspect(root: Path) -> tuple[dict[str, object], int]:
    root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    config = risk_gate_config.load_machine_config()
    required = risk_gate_config.validate_pr_checks(config)
    if not required:
        raise PRCheckError("No required PR checks are configured in machine config.")
    ship_config = config.get("ship", {})
    if not isinstance(ship_config, dict):
        raise PRCheckError("ship machine config is invalid.")
    remote = ship_config.get("remote", "origin")
    if not isinstance(remote, str) or remote != "origin":
        raise PRCheckError("PR checks require the origin remote.")
    _, repo = ship_workflow.require_github_remote(root, remote)
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise PRCheckError("Detached HEAD has no pull request.")
    local_head = _git(root, "rev-parse", "HEAD")
    remote_head = _remote_head(root, remote, branch)
    if local_head != remote_head:
        raise PRCheckError("Local HEAD does not match the remote branch HEAD.")
    pr = ship_workflow.pull_request(root, repo, branch, local_head)
    # pull_request validates OPEN state, branch identity, and PR head SHA.
    result_rows, sha_matches = github_adapter.check_results(root, repo, local_head)
    if not sha_matches:
        raise PRCheckError("GitHub returned check results for a different HEAD SHA.")

    latest: dict[str, dict[str, Any]] = {}
    for row in result_rows:
        # GitHub returns newer check runs first; retain the first matching row.
        latest.setdefault(row["name"], row)
    missing = [name for name in required if name not in latest]
    selected = [latest[name] for name in required if name in latest]
    pending_states = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
    if missing or any(row["status"] in pending_states for row in selected):
        return {"status": "PENDING", "prNumber": pr["prNumber"], "headSha": local_head}, EXIT_CODES["PENDING"]

    public_checks = [{
        "name": github_adapter._safe_text(row["name"])[:120], "status": "COMPLETED", "conclusion": row["conclusion"],
    } for row in selected]
    failures: list[dict[str, object]] = []
    outcomes: list[str] = []
    for row in selected:
        is_risk_gate_assess = row["name"].strip().lower() == "risk-gate / assess"
        if is_risk_gate_assess and row["conclusion"] == "SUCCESS":
            continue
        protocol_text = " ".join((row.get("summary", ""), row.get("text", "")))
        protocol = _protocol(protocol_text)
        if row["conclusion"] == "SUCCESS" and not (protocol in {"BLOCK", "REVIEW", "ERROR"}):
            continue
        if row["status"] != "COMPLETED" or not row["conclusion"]:
            raise PRCheckError("A required check returned an invalid completed state.")
        steps = github_adapter.failure_steps(root, row.get("runId"))
        outcome, category, summary = _classify_failure(row, steps)
        if is_risk_gate_assess and row["conclusion"] == "FAILURE" and category == "RISK_GATE":
            decision = github_adapter.risk_gate_decision(root, row.get("runId"))
            if decision == "BLOCK":
                outcome, summary = "REPAIRABLE_FAILURE", "Risk Gate returned BLOCK."
            elif decision == "REVIEW":
                outcome, summary = "NEEDS_REVIEW", "Risk Gate requires human review."
            elif decision == "ERROR":
                outcome, summary = "ERROR", "Risk Gate execution failed."
            elif decision == "PASS":
                outcome, summary = "ERROR", "Risk Gate check failed despite a PASS decision."
            else:
                outcome, summary = "NEEDS_REVIEW", "Risk Gate outcome is unavailable."
        outcomes.append(outcome)
        failures.append(_failure_evidence(row, category, summary, steps))
    if not failures:
        return {
            "status": "PASS", "prNumber": pr["prNumber"], "headSha": local_head,
            "checks": public_checks,
        }, EXIT_CODES["PASS"]
    status = "ERROR" if "ERROR" in outcomes else "NEEDS_REVIEW" if "NEEDS_REVIEW" in outcomes else "REPAIRABLE_FAILURE"
    payload: dict[str, object] = {"status": status, "prNumber": pr["prNumber"], "headSha": local_head}
    if status == "REPAIRABLE_FAILURE":
        payload["failures"] = failures
    elif status == "NEEDS_REVIEW":
        payload["reasons"] = failures
    else:
        payload["reasons"] = failures
    return payload, EXIT_CODES[status]
