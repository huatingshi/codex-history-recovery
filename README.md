# Codex History Recovery

A Codex skill for diagnosing and repairing hidden local Codex Desktop/CLI chat history after switching accounts, providers, API login modes, or `model_provider` values.

The bundled script is conservative by default:

- `status` is read-only.
- `apply` creates timestamped backups before changing SQLite or JSONL metadata.
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

## What It Fixes

- `threads.model_provider` bucket drift in `state_5.sqlite`.
- `threads.cwd` values using Windows `\\?\` extended path prefixes.
- Stale `session_meta.payload.model_provider` and `session_meta.payload.cwd` metadata in Codex session JSONL files.
- Multiple current state DB locations, including `.codex\sqlite\state_5.sqlite` and `.codex\state_5.sqlite`.

## Safety

The repair never deletes sessions or databases and never overwrites `auth.json`. Backups are written under:

```text
%USERPROFILE%\.codex\backups_state\codex-history-recovery\
```

