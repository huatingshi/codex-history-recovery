# Codex History Recovery

A Codex skill for diagnosing and repairing hidden local Codex Desktop/CLI chat history after switching accounts, providers, API login modes, or `model_provider` values.

The bundled script is conservative by default:

- `status` is read-only.
- `apply` performs a read-only preflight and skips backup creation when nothing needs repair.
- Full recovery creates a timestamped baseline before changing SQLite or JSONL metadata.
- `apply --thread-id <id>` repairs only one thread and appends a small pre-write snapshot to the latest complete baseline.
- Message content, titles, timestamps, IDs, and rollout paths are left untouched.

## Usage

Run a read-only status check first:

```powershell
python "$env:USERPROFILE\.codex\skills\codex-history-recovery\scripts\recover_codex_history.py" status
```

If the output shows provider or Windows extended-path mismatches, fully quit Codex Desktop and apply the repair:

```powershell
python "$env:USERPROFILE\.codex\skills\codex-history-recovery\scripts\recover_codex_history.py" apply
```

Use `--allow-running` only if you accept that a running Codex app may rewrite the database while the repair is happening.

For a known thread, use the fast scoped path:

```powershell
python "$env:USERPROFILE\.codex\skills\codex-history-recovery\scripts\recover_codex_history.py" apply --thread-id <thread-id>
```

Provider-only metadata changes with equal byte length update just the first `session_meta` line. Length-changing metadata uses a validated streaming replacement. In both cases all message bytes after the first metadata record remain unchanged.

## What It Fixes

- `threads.model_provider` bucket drift in `state_5.sqlite`.
- `threads.cwd` values using Windows `\\?\` extended path prefixes.
- Stale `session_meta.payload.model_provider` and `session_meta.payload.cwd` metadata in Codex session JSONL files.
- Multiple current state DB locations, including `.codex\sqlite\state_5.sqlite` and `.codex\state_5.sqlite`.

## Safety

The repair never deletes sessions or databases and never overwrites `auth.json`. On Windows, backups are written under:

```text
D:\CodexBackups\codex-history-recovery\
```

Override this with `CODEX_HISTORY_BACKUP_DIR` or `--backup-root`. Only the newest validated top-level backup is retained by default. Scoped repairs store exact pre-write snapshots inside that backup's `increments\` directory.
