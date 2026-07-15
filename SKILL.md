---
name: codex-history-recovery
description: Diagnose and repair hidden Codex Desktop/CLI local chat history after switching account, API login, ccswitch, custom provider, or model_provider. Use when chats disappear from the sidebar/resume list even though local .codex session files remain, especially provider bucket drift, Windows \\?\ cwd path mismatches, or multiple state_5.sqlite locations under .codex and .codex/sqlite.
---

# Codex History Recovery

Use this skill when Codex local conversations disappear after switching provider/login, but the user suspects the records still exist locally.

## Core Lessons

- Do not assume chats are deleted. Check local rollout JSONL files and SQLite first.
- On current Codex Desktop for Windows, the active state DB may be `C:\Users\<user>\.codex\sqlite\state_5.sqlite`, not only `C:\Users\<user>\.codex\state_5.sqlite`.
- The Desktop sidebar can hide valid threads for two independent reasons:
  - `threads.model_provider` does not match the current `model_provider` in `config.toml`.
  - `threads.cwd` uses Windows extended paths like `\\?\D:\...` while saved project roots use `D:\...`.
- Repair both active SQLite metadata and rollout JSONL `session_meta.payload` metadata. If only SQLite is edited, Codex may later read stale metadata back from rollout files.
- Always create timestamped backups before writing.
- Because chat backups can be several GB, keeping only the latest recovery backup is acceptable when the new backup passes lightweight coverage checks and covers JSONL session ids/paths from older backups.

## Preferred Workflow

1. Run the bundled script in status mode:

```powershell
python "$env:USERPROFILE\.codex\skills\codex-history-recovery\scripts\recover_codex_history.py" status
```

2. Inspect the output:
   - Current provider from `config.toml`.
   - Provider counts in both `state_5.sqlite` candidates.
   - CWD prefix counts: `extended` means `\\?\` paths.
   - Exact matches between `threads.cwd` and `.codex-global-state.json` saved roots.
   - Which DB is likely active, usually `.codex\sqlite\state_5.sqlite` if present and recently modified.

3. If the issue is provider/cwd mismatch, run apply:

```powershell
python "$env:USERPROFILE\.codex\skills\codex-history-recovery\scripts\recover_codex_history.py" apply
```

4. Prefer applying while Codex Desktop is fully quit. The script blocks by default when Codex processes are running; use `--allow-running` only when the user accepts that Desktop may rewrite the DB while open.

5. Ask the user to fully quit and reopen Codex Desktop. If UI still does not update, check whether a running app-server rewrote the active DB or whether a different DB path became active.

6. When pruning old recovery backups, keep the default `--keep-backups 1` unless the user asks otherwise. The script should refuse to prune if the newest backup does not contain readable SQLite copies or does not cover current/older JSONL session ids or fallback paths.

## Safety Rules

- Never delete sessions or databases for this recovery.
- Never overwrite `auth.json`.
- Never change message content, titles, timestamps, IDs, or rollout paths unless explicitly requested.
- Never scan full message bodies or hash multi-GB session trees just to validate backup coverage. Prefer cheap checks: file listings, JSONL first-line `session_meta.payload.id`, fallback relative paths, and opening SQLite copies to count `threads`.
- Prefer script `status` before `apply`.
- If applying manually, repair both:
  - `~/.codex/sqlite/state_5.sqlite` when present.
  - `~/.codex/state_5.sqlite` when present.
  - `~/.codex/sessions/**/*.jsonl` and `~/.codex/archived_sessions/**/*.jsonl` `session_meta.payload.model_provider` and `session_meta.payload.cwd`.
- If `status` shows the active DB returning to `extended` paths after a successful repair, close every `Codex.exe`, `codex.exe`, and `node_repl.exe` process, then run `apply` again.

## Expected Fix

For the common Windows/API-login case, the repair should:

- Set all `threads.model_provider` to the current provider, e.g. `sub2api`.
- Remove leading `\\?\` from `threads.cwd`.
- Set JSONL `session_meta.payload.model_provider` to the current provider.
- Remove leading `\\?\` from JSONL `session_meta.payload.cwd`.
- Leave messages and user content untouched.

## Validation

After repair, verify:

- Active DB provider count is one bucket matching the current provider.
- Active DB CWD prefix count is `normal`.
- Saved project roots have nonzero exact matches.
- A known missing thread has the expected provider and project root.

Useful targeted query via the script:

```powershell
python "$env:USERPROFILE\.codex\skills\codex-history-recovery\scripts\recover_codex_history.py" status --thread-id 019ec3b8-7320-7f63-9291-370d43659500
```
