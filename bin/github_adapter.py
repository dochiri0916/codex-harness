"""Structured GitHub PR and check data used by harness commands."""

from __future__ import annotations

import json
import re
from typing import Any

import ship_workflow


class GitHubError(Exception):
    pass


def _json_command(root, *args: str, label: str) -> Any:
    result = ship_workflow.github(root, *args)
    if result.returncode:
        raise GitHubError(f"Could not read {label} from GitHub.")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GitHubError(f"GitHub returned malformed {label} data.") from exc


def check_results(root, repo: str, sha: str) -> tuple[list[dict[str, Any]], bool]:
    """Return normalized check results and whether every result names this SHA."""
    runs = _json_command(
        root, "api", f"repos/{repo}/commits/{sha}/check-runs", "-f", "per_page=100",
        label="check runs",
    )
    statuses = _json_command(
        root, "api", f"repos/{repo}/commits/{sha}/status", label="commit statuses",
    )
    if not isinstance(runs, dict) or not isinstance(runs.get("check_runs"), list):
        raise GitHubError("GitHub returned malformed check run data.")
    if not isinstance(statuses, dict) or not isinstance(statuses.get("statuses"), list):
        raise GitHubError("GitHub returned malformed commit status data.")

    normalized: list[dict[str, Any]] = []
    sha_matches = True
    for run in runs["check_runs"]:
        if not isinstance(run, dict):
            raise GitHubError("GitHub returned malformed check run data.")
        name, status, conclusion, result_sha = (
            run.get("name"), run.get("status"), run.get("conclusion"), run.get("head_sha")
        )
        if not all(isinstance(value, str) and value for value in (name, status, result_sha)):
            raise GitHubError("GitHub returned incomplete check run data.")
        if status.lower() not in {"queued", "in_progress", "completed", "waiting", "requested", "pending"}:
            raise GitHubError("GitHub returned an unknown check run status.")
        sha_matches = sha_matches and result_sha == sha
        output = run.get("output", {})
        if output is not None and not isinstance(output, dict):
            raise GitHubError("GitHub returned malformed check output data.")
        normalized.append({
            "name": name,
            "status": status.upper(),
            "conclusion": conclusion.upper() if isinstance(conclusion, str) else None,
            "headSha": result_sha,
            "summary": _safe_text(output.get("summary", "")) if isinstance(output, dict) else "",
            "text": _safe_text(output.get("text", "")) if isinstance(output, dict) else "",
            "detailsUrl": run.get("details_url") if isinstance(run.get("details_url"), str) else "",
            "runId": _run_id(run),
        })
    for status in statuses["statuses"]:
        if not isinstance(status, dict):
            raise GitHubError("GitHub returned malformed commit status data.")
        name, state, result_sha = status.get("context"), status.get("state"), status.get("sha")
        if not all(isinstance(value, str) and value for value in (name, state, result_sha)):
            raise GitHubError("GitHub returned incomplete commit status data.")
        sha_matches = sha_matches and result_sha == sha
        state_upper = state.upper()
        if state_upper not in {"PENDING", "SUCCESS", "FAILURE", "ERROR"}:
            raise GitHubError("GitHub returned an unknown commit status.")
        normalized.append({
            "name": name,
            "status": "COMPLETED" if state_upper in {"SUCCESS", "FAILURE", "ERROR"} else "IN_PROGRESS",
            "conclusion": state_upper if state_upper in {"SUCCESS", "FAILURE", "ERROR"} else None,
            "headSha": result_sha,
            "summary": "",
            "text": _safe_text(status.get("description", "")),
            "detailsUrl": status.get("target_url") if isinstance(status.get("target_url"), str) else "",
            "runId": None,
        })
    return normalized, sha_matches


def _run_id(run: dict[str, Any]) -> str | None:
    external_id = run.get("external_id")
    if isinstance(external_id, str):
        match = re.match(r"(\d+)(?:_|$)", external_id)
        if match:
            return match.group(1)
    details = run.get("details_url")
    if isinstance(details, str):
        match = re.search(r"/actions/runs/(\d+)", details)
        if match:
            return match.group(1)
    return None


def failure_steps(root, run_id: str | None) -> list[str]:
    """Read only failed job and step names from one Actions run."""
    if not run_id:
        return []
    result = ship_workflow.github(root, "run", "view", run_id, "--json", "jobs")
    if result.returncode:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return []
    failed: list[str] = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("conclusion") not in {"failure", "timed_out", "cancelled"}:
            continue
        steps = job.get("steps", [])
        names = [
            step.get("name") for step in steps
            if isinstance(step, dict) and step.get("conclusion") in {"failure", "timed_out", "cancelled"}
            and isinstance(step.get("name"), str)
        ] if isinstance(steps, list) else []
        failed.extend(names or ([job["name"]] if isinstance(job.get("name"), str) else []))
    return [_safe_text(name)[:120] for name in failed[:8]]


def _safe_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"(?im)(authorization\s*['\"]?\s*:\s*)[^\r\n,}]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(authorization\s*:\s*bearer\s+|bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b|\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    value = re.sub(r"(?i)(token|secret|password|api[_-]?key)(\s*[=:]\s*)[^\s,;]+", r"\1\2[REDACTED]", value)
    return value[:4000]
