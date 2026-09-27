# Codex Harness

Local lifecycle hooks that run deterministic checks only when a Codex turn changes repository files. The harness does not replace or wrap the installed `codex` executable.

## Install

1. Keep this repository at a stable local path. The checked-in `hooks.json` currently points to `/Users/seongbin/programming/utility/codex-harness`; update both command paths if the repository is elsewhere.
2. If `~/.codex/hooks.json` does not exist, install this configuration:

   ```sh
   mkdir -p ~/.codex
   cp /Users/seongbin/programming/utility/codex-harness/hooks.json ~/.codex/hooks.json
   ```

   If it already exists, merge the `UserPromptSubmit`, `PreToolUse`, and `Stop` entries into its `hooks` object instead of replacing the file.

3. Ensure Python 3 is available. Hook state is stored under `~/.local/state/codex-harness/`, outside repositories. Set `CODEX_HARNESS_STATE_DIR` to use another private state directory.

4. Configure the Risk Gate repository and optional explicit ship command in machine-local config at `~/.config/codex-harness/config.json`:

   ```sh
   mkdir -p ~/.config/codex-harness
   cat > ~/.config/codex-harness/config.json <<'EOF'
   {"riskGateHome":"/absolute/path/to/risk-gate","jev":{"enabled":true,"keychainService":"risk-gate-typesafe-api-key"},"ship":{"enabled":false,"remote":"origin"}}
   EOF
   ```

   `RISK_GATE_HOME` remains supported and takes precedence when it points to a valid Risk Gate installation. If neither source is configured, the hook reports `NOT_CONFIGURED`.

When Jev is enabled, `TYPESAFE_API_KEY` from the environment takes precedence. On macOS, if it is absent, the hook reads the configured generic-password entry from Keychain and passes the value only to the Risk Gate child process. If Jev is disabled, it is disabled for that process.

Codex will request trust for the global hooks before running them. Review and trust this hook definition in Codex. `hooks.json` uses the current command-hook schema for `UserPromptSubmit`, `PreToolUse`, and `Stop`.

## Behavior

- `PreToolUse` snapshots tracked file contents, index entries, and untracked files immediately before the first `Bash`, `apply_patch`, `Edit`, or `Write` tool in each turn. `UserPromptSubmit` remains an advisory fallback. Snapshots and failed-snapshot markers are keyed by Codex session/turn and written outside repositories.
- `stop_verify.py` compares that snapshot with the working tree. Only files whose index or working content changed during the current prompt are considered.
- `README.md`, `README.*`, `CHANGELOG.md`, `docs/**`, and `**/*.md` are docs-only patterns. Every other or unknown path triggers verification.
- The Stop hook treats a turn with no snapshot and no attempted mutating-capable local tool as unchanged. Build Convention Gradle projects run `./gradlew check` once per Stop, then require `build/reports/build-convention/report.json` before running the configured Local Risk Gate. PASS permits completion; BLOCK sends its `reasonCodes` to Codex and uses the five-attempt continuation limit. REVIEW and tool errors stop with `NEEDS_REVIEW`. A successful Risk Gate and Jev PASS records a private machine-local validation attestation outside the repository.
- The Harness repository runs `./bin/check` (Harness tests and Python syntax checks) and does not run Local Risk Gate. Generic projects without a supported verifier are skipped. Docs-only changes skip verification.
- `bin/ship` is a separate explicit lifecycle command. It is disabled unless `ship.enabled` is true, and requires a matching Stop-hook attestation, a clean-start Codex session, a GitHub `origin`, and a non-default feature branch. It commits the verified tree, pushes without force, then reuses an open branch PR or creates one. Use `bin/ship --message "..." --summary "..."` to supply a commit message and short PR title.

## Tests

Run `python3 -m pytest -q` from this repository.
