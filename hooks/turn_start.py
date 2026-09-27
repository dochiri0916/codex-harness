#!/usr/bin/env python3
"""Capture the repository state at the start of a Codex prompt turn."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def state_dir() -> Path:
    configured = os.environ.get("CODEX_HARNESS_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "codex-harness"


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


def repository_snapshot(cwd: Path) -> tuple[str, dict[str, dict[str, object]]] | None:
    try:
        root = Path(os.fsdecode(git(cwd, "rev-parse", "--show-toplevel").strip())).resolve()
        tracked = set(git(root, "ls-files", "-z").split(b"\0")) - {b""}
        try:
            tracked.update(set(git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD").split(b"\0")) - {b""})
        except subprocess.CalledProcessError:
            pass
        untracked = set(git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")) - {b""}
        index_entries: dict[bytes, list[str]] = {}
        for item in git(root, "ls-files", "--stage", "-z").split(b"\0"):
            if not item:
                continue
            metadata, raw_path = item.split(b"\t", 1)
            index_entries.setdefault(raw_path, []).append(metadata.decode("ascii", "replace"))
        paths = tracked | untracked
        snapshot: dict[str, dict[str, object]] = {}
        for raw_path in paths:
            name = os.fsdecode(raw_path)
            snapshot[name] = {
                "tracked": raw_path in tracked,
                "index": sorted(index_entries.get(raw_path, [])),
                "worktree": digest(root / name),
            }
        return str(root), snapshot
    except (subprocess.CalledProcessError, OSError):
        return None


def write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
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


def main() -> int:
    try:
        event = json.load(sys.stdin)
        session_id = event.get("session_id")
        turn_id = event.get("turn_id")
        cwd = event.get("cwd")
        if not all(isinstance(value, str) and value for value in (session_id, turn_id, cwd)):
            return 0
        key = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()
        path = state_dir() / f"{key}.json"
        retry_marker = f"[[CODEX_HARNESS_RETRY:{key[:12]}]]"
        prompt = event.get("prompt")
        if isinstance(prompt, str) and prompt.startswith(retry_marker) and path.is_file():
            # Stop-hook continuation prompts are internally submitted as user prompts.
            # Keep the original baseline and failure count across those retries.
            return 0
        result = repository_snapshot(Path(cwd))
        if result is None:
            return 0
        root, files = result
        write_state(path, {
            "session_id": session_id,
            "turn_id": turn_id,
            "root": root,
            "files": files,
            "failures": 0,
        })
    except (ValueError, OSError, json.JSONDecodeError):
        # Snapshotting is advisory; never prevent the prompt from being submitted.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
