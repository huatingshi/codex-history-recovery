#!/usr/bin/env python3
"""Diagnose and repair hidden Codex local history metadata.

Default command is read-only status. The apply command creates timestamped
backups before touching SQLite or rollout JSONL metadata.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from typing import Any


EXTENDED_PREFIX = "\\\\?\\"
SESSION_DIRS = ("sessions", "archived_sessions")
DEFAULT_BACKUP_ROOT_WINDOWS = pathlib.Path("D:/CodexBackups/codex-history-recovery")


def default_codex_home() -> pathlib.Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path.home() / ".codex"


def default_backup_root(codex_home: pathlib.Path) -> pathlib.Path:
    configured = os.environ.get("CODEX_HISTORY_BACKUP_DIR")
    if configured:
        return pathlib.Path(configured).expanduser()
    if os.name == "nt":
        return DEFAULT_BACKUP_ROOT_WINDOWS
    return codex_home / "backups_state" / "codex-history-recovery"


def load_current_provider(codex_home: pathlib.Path) -> str:
    config = codex_home / "config.toml"
    text = config.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"\s*$', text)
    if not match:
        raise RuntimeError(f"Could not find root model_provider in {config}")
    return match.group(1)


def candidate_state_dbs(codex_home: pathlib.Path) -> list[pathlib.Path]:
    candidates = [
        codex_home / "sqlite" / "state_5.sqlite",
        codex_home / "state_5.sqlite",
    ]
    return [p for p in candidates if p.exists()]


def read_global_roots(codex_home: pathlib.Path) -> list[str]:
    path = codex_home / ".codex-global-state.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    roots = data.get("electron-saved-workspace-roots", [])
    return [r for r in roots if isinstance(r, str)]


def connect_ro(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def summarize_db(path: pathlib.Path, roots: list[str], thread_id: str | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        if path.exists()
        else None,
    }
    if not path.exists():
        return info
    con = connect_ro(path)
    try:
        tables = [r["name"] for r in con.execute("select name from sqlite_master where type='table'")]
        info["has_threads"] = "threads" in tables
        if "threads" not in tables:
            return info
        info["total_threads"] = con.execute("select count(*) from threads").fetchone()[0]
        info["provider_counts"] = {
            r["provider"]: r["n"]
            for r in con.execute(
                "select coalesce(model_provider,'<NULL>') provider, count(*) n "
                "from threads group by coalesce(model_provider,'<NULL>') order by n desc"
            )
        }
        info["cwd_prefix_counts"] = {
            r["kind"]: r["n"]
            for r in con.execute(
                "select case when cwd like '\\\\?\\%' then 'extended' "
                "when cwd is null then 'null' else 'normal' end kind, count(*) n "
                "from threads group by kind"
            )
        }
        exact = []
        for root in roots:
            n = con.execute(
                "select count(*) from threads where archived=0 and cwd=?",
                (root,),
            ).fetchone()[0]
            n_ext = con.execute(
                "select count(*) from threads where archived=0 and cwd=?",
                (EXTENDED_PREFIX + root,),
            ).fetchone()[0]
            if n or n_ext:
                exact.append({"root": root, "normal": n, "extended": n_ext})
        info["root_matches"] = exact
        if thread_id:
            row = con.execute(
                "select id, model_provider, cwd, source, thread_source, archived, title "
                "from threads where id=?",
                (thread_id,),
            ).fetchone()
            info["thread"] = dict(row) if row else None
    finally:
        con.close()
    return info


def summarize_jsonl(codex_home: pathlib.Path, thread_id: str | None) -> dict[str, Any]:
    roots = [codex_home / "sessions", codex_home / "archived_sessions"]
    provider_counts: Counter[str] = Counter()
    cwd_prefix_counts: Counter[str] = Counter()
    files_seen = 0
    thread_file: dict[str, Any] | None = None
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            files_seen += 1
            try:
                first = first_json_line(path)
            except Exception:
                continue
            if first.get("type") != "session_meta":
                continue
            payload = first.get("payload") or {}
            provider_counts[str(payload.get("model_provider", "<NULL>"))] += 1
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd.startswith(EXTENDED_PREFIX):
                cwd_prefix_counts["extended"] += 1
            elif cwd is None:
                cwd_prefix_counts["null"] += 1
            else:
                cwd_prefix_counts["normal"] += 1
            if thread_id and payload.get("id") == thread_id:
                thread_file = {
                    "path": str(path),
                    "provider": payload.get("model_provider"),
                    "cwd": payload.get("cwd"),
                }
    return {
        "files_seen": files_seen,
        "provider_counts": dict(provider_counts),
        "cwd_prefix_counts": dict(cwd_prefix_counts),
        "thread_file": thread_file,
    }


def running_codex_processes() -> list[dict[str, str]]:
    names = ("codex.exe", "codex", "node_repl.exe", "node_repl")
    found: list[dict[str, str]] = []
    if os.name == "nt":
        try:
            output = subprocess.check_output(
                ["tasklist", "/fo", "csv", "/nh"],
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            return found
        for raw in output.splitlines():
            parts = [p.strip('"') for p in raw.split('","')]
            if not parts:
                continue
            image = parts[0].strip('"')
            if image.lower() in names:
                found.append({"image": image, "pid": parts[1].strip('"') if len(parts) > 1 else ""})
    else:
        try:
            output = subprocess.check_output(
                ["ps", "-eo", "pid=,comm="],
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            return found
        for line in output.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            pid, comm = parts
            if pathlib.Path(comm).name.lower() in names:
                found.append({"image": comm, "pid": pid})
    return found


def first_json_line(path: pathlib.Path) -> dict[str, Any]:
    record = read_first_json_record(path)
    return record["item"] if record else {}


def read_first_json_record(path: pathlib.Path) -> dict[str, Any] | None:
    """Read only the first non-empty JSON line and retain its byte location."""
    with path.open("rb") as handle:
        while True:
            offset = handle.tell()
            raw = handle.readline()
            if not raw:
                return None
            if not raw.strip():
                continue
            body = raw.rstrip(b"\r\n")
            newline = raw[len(body) :]
            return {
                "offset": offset,
                "raw": raw,
                "body": body,
                "newline": newline,
                "item": json.loads(body.decode("utf-8")),
            }


def session_meta_change(path: pathlib.Path, provider: str) -> dict[str, Any] | None:
    record = read_first_json_record(path)
    if not record:
        return None
    item = record["item"]
    payload = item.get("payload") if isinstance(item, dict) else None
    if item.get("type") != "session_meta" or not isinstance(payload, dict):
        return None
    before_provider = payload.get("model_provider")
    before_cwd = payload.get("cwd")
    after_cwd = (
        before_cwd[len(EXTENDED_PREFIX) :]
        if isinstance(before_cwd, str) and before_cwd.startswith(EXTENDED_PREFIX)
        else before_cwd
    )
    if before_provider == provider and before_cwd == after_cwd:
        return None
    return {
        "path": path,
        "thread_id": payload.get("id"),
        "before_provider": before_provider,
        "before_cwd": before_cwd,
        "after_provider": provider,
        "after_cwd": after_cwd,
    }


def iter_jsonl_paths(codex_home: pathlib.Path):
    for dirname in SESSION_DIRS:
        root = codex_home / dirname
        if root.exists():
            yield from root.rglob("*.jsonl")


def normalize_rollout_path(raw: str) -> pathlib.Path:
    if raw.startswith(EXTENDED_PREFIX):
        raw = raw[len(EXTENDED_PREFIX) :]
    return pathlib.Path(raw)


def find_thread_jsonl_paths(codex_home: pathlib.Path, thread_id: str) -> list[pathlib.Path]:
    found: set[pathlib.Path] = set()
    home_resolved = codex_home.resolve()
    for db in candidate_state_dbs(codex_home):
        con = connect_ro(db)
        try:
            row = con.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()
            if row and isinstance(row["rollout_path"], str):
                candidate = normalize_rollout_path(row["rollout_path"])
                try:
                    candidate.resolve().relative_to(home_resolved)
                except ValueError:
                    continue
                if candidate.is_file():
                    found.add(candidate)
        finally:
            con.close()
    valid = []
    for path in found:
        try:
            first = first_json_line(path)
        except Exception:
            # Preserve the DB-resolved target so preflight can report the parse error.
            valid.append(path)
            continue
        payload = first.get("payload") if isinstance(first, dict) else None
        if isinstance(payload, dict) and payload.get("id") == thread_id:
            valid.append(path)
    if valid:
        return sorted(valid)
    for path in iter_jsonl_paths(codex_home):
        try:
            first = first_json_line(path)
        except Exception:
            continue
        payload = first.get("payload") if isinstance(first, dict) else None
        if isinstance(payload, dict) and payload.get("id") == thread_id:
            valid.append(path)
    return sorted(set(valid))


def collect_jsonl_changes(
    codex_home: pathlib.Path, provider: str, thread_id: str | None
) -> dict[str, Any]:
    paths = (
        find_thread_jsonl_paths(codex_home, thread_id)
        if thread_id
        else list(iter_jsonl_paths(codex_home))
    )
    changes = []
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            change = session_meta_change(path, provider)
        except Exception as exc:
            errors.append({"path": str(path), "error": repr(exc)})
            continue
        if change:
            changes.append(change)
    return {
        "files_seen": len(paths),
        "files_to_change": len(changes),
        "changes": changes,
        "errors": errors,
    }


def backup_sqlite(src: pathlib.Path, dst: pathlib.Path) -> None:
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(str(dst))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    for suffix in ("-wal", "-shm"):
        side = pathlib.Path(str(src) + suffix)
        if side.exists():
            shutil.copy2(side, dst.parent / side.name)


def make_backup(codex_home: pathlib.Path, backup_root: pathlib.Path | None = None) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    root = backup_root or default_backup_root(codex_home)
    backup = root / stamp
    backup.mkdir(parents=True, exist_ok=False)
    for db in candidate_state_dbs(codex_home):
        rel = db.relative_to(codex_home)
        out = backup / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        backup_sqlite(db, out)
    for name in ("config.toml", "session_index.jsonl", ".codex-global-state.json"):
        src = codex_home / name
        if src.exists():
            shutil.copy2(src, backup / name)
    for dirname in ("sessions", "archived_sessions"):
        src = codex_home / dirname
        if src.exists():
            shutil.copytree(src, backup / dirname)
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "codex_home": str(codex_home),
                "note": "Backup before codex-history-recovery apply.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return backup


def latest_complete_backup(
    codex_home: pathlib.Path, backup_root: pathlib.Path
) -> pathlib.Path | None:
    if not backup_root.exists():
        return None
    candidates = sorted(
        (path for path in backup_root.iterdir() if path.is_dir() and not path.is_symlink()),
        key=lambda path: path.name,
        reverse=True,
    )
    for path in candidates:
        if backup_completeness(path, codex_home)["complete"]:
            return path
    return None


def make_incremental_backup(
    codex_home: pathlib.Path,
    baseline: pathlib.Path,
    thread_id: str,
    jsonl_paths: list[pathlib.Path],
) -> tuple[pathlib.Path, dict[str, Any]]:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    increment = baseline / "increments" / f"{stamp}-{thread_id}"
    increment.mkdir(parents=True, exist_ok=False)
    copied: list[dict[str, Any]] = []
    try:
        for db in candidate_state_dbs(codex_home):
            rel = db.relative_to(codex_home)
            dst = increment / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            backup_sqlite(db, dst)
            copied.append({"path": rel.as_posix(), "bytes": dst.stat().st_size, "kind": "sqlite"})
        for src in jsonl_paths:
            rel = src.relative_to(codex_home)
            dst = increment / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append({"path": rel.as_posix(), "bytes": dst.stat().st_size, "kind": "jsonl"})
        config = codex_home / "config.toml"
        if config.exists():
            shutil.copy2(config, increment / "config.toml")
        db_health = [
            sqlite_backup_health(increment / db.relative_to(codex_home))
            for db in candidate_state_dbs(codex_home)
        ]
        jsonl_ok = all(
            (increment / path.relative_to(codex_home)).stat().st_size == path.stat().st_size
            for path in jsonl_paths
        )
        health = {
            "complete": jsonl_ok and all(item.get("ok") for item in db_health),
            "thread_id": thread_id,
            "files": copied,
            "db_health": db_health,
        }
        if not health["complete"]:
            raise RuntimeError("Targeted backup did not pass pre-write validation")
        (increment / "manifest.json").write_text(
            json.dumps(
                {
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "scope": "thread",
                    "thread_id": thread_id,
                    "codex_home": str(codex_home),
                    **health,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return increment, health
    except Exception:
        shutil.rmtree(increment, ignore_errors=True)
        raise


def backup_tree_stats(path: pathlib.Path) -> dict[str, Any]:
    bytes_seen = 0
    files_seen = 0
    jsonl_seen = 0
    sqlite_seen = 0
    latest = path.stat().st_mtime
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        bytes_seen += stat.st_size
        files_seen += 1
        if item.suffix == ".jsonl":
            jsonl_seen += 1
        if item.suffix == ".sqlite":
            sqlite_seen += 1
        latest = max(latest, stat.st_mtime)
    return {
        "bytes": bytes_seen,
        "files": files_seen,
        "jsonl": jsonl_seen,
        "sqlite": sqlite_seen,
        "latest_mtime": latest,
        "latest_time": dt.datetime.fromtimestamp(latest).isoformat(),
    }


def sorted_sample(values: set[str], limit: int = 5) -> list[str]:
    return sorted(values)[:limit]


def jsonl_identity_keys(root: pathlib.Path) -> set[str]:
    keys: set[str] = set()
    for dirname in SESSION_DIRS:
        base = root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*.jsonl"):
            rel = path.relative_to(root).as_posix()
            key = f"path:{rel}"
            try:
                first = first_json_line(path)
                payload = first.get("payload") if isinstance(first, dict) else None
                thread_id = payload.get("id") if isinstance(payload, dict) else None
                if isinstance(thread_id, str) and thread_id:
                    key = f"id:{thread_id}"
            except Exception:
                pass
            keys.add(key)
    return keys


def sqlite_backup_health(path: pathlib.Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.exists() else 0,
        "ok": False,
    }
    if not path.is_file() or info["size"] == 0:
        info["error"] = "missing or empty"
        return info
    try:
        con = connect_ro(path)
        try:
            tables = [r["name"] for r in con.execute("select name from sqlite_master where type='table'")]
            info["has_threads"] = "threads" in tables
            if "threads" in tables:
                info["threads"] = con.execute("select count(*) from threads").fetchone()[0]
            info["ok"] = True
        finally:
            con.close()
    except Exception as exc:
        info["error"] = repr(exc)
    return info


def backup_source_coverage(backup: pathlib.Path, codex_home: pathlib.Path) -> dict[str, Any]:
    source_keys = jsonl_identity_keys(codex_home)
    backup_keys = jsonl_identity_keys(backup)
    missing_keys = source_keys - backup_keys
    db_health = []
    db_ok = True
    for db in candidate_state_dbs(codex_home):
        dst = backup / db.relative_to(codex_home)
        health = sqlite_backup_health(dst)
        db_health.append(health)
        db_ok = db_ok and bool(health.get("ok"))
    return {
        "complete": db_ok and not missing_keys,
        "source_jsonl_keys": len(source_keys),
        "backup_jsonl_keys": len(backup_keys),
        "missing_jsonl_keys": len(missing_keys),
        "missing_jsonl_sample": sorted_sample(missing_keys),
        "db_health": db_health,
    }


def backup_completeness(backup: pathlib.Path, codex_home: pathlib.Path) -> dict[str, Any]:
    missing: list[str] = []
    for name in ("manifest.json", "apply-report.json"):
        if not (backup / name).is_file():
            missing.append(name)

    for dirname in SESSION_DIRS:
        src = codex_home / dirname
        if src.exists() and not (backup / dirname).is_dir():
            missing.append(f"{dirname}/")

    dbs = candidate_state_dbs(codex_home)
    if not dbs:
        missing.append("state_5.sqlite source")
    db_health = []
    for db in dbs:
        dst = backup / db.relative_to(codex_home)
        health = sqlite_backup_health(dst)
        db_health.append(health)
        if not health.get("ok"):
            missing.append(str(dst.relative_to(backup)))

    stats = backup_tree_stats(backup)
    if stats["sqlite"] == 0:
        missing.append("*.sqlite")
    if stats["jsonl"] == 0:
        missing.append("*.jsonl")

    return {
        **stats,
        "path": str(backup),
        "complete": not missing,
        "missing": missing,
        "db_health": db_health,
    }


def prune_history_backups(
    codex_home: pathlib.Path,
    keep_backups: int,
    backup_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    if keep_backups < 1:
        raise ValueError("--keep-backups must be at least 1")

    backup_root = backup_root or default_backup_root(codex_home)
    if not backup_root.exists():
        return {"backup_root": str(backup_root), "kept": [], "deleted": [], "skipped": "backup root missing"}

    root_resolved = backup_root.resolve()
    backups = [
        path
        for path in backup_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    ]
    inspected = [backup_completeness(path, codex_home) for path in backups]
    complete = [info for info in inspected if info["complete"]]
    if not complete:
        return {
            "backup_root": str(backup_root),
            "kept": [],
            "deleted": [],
            "inspected": inspected,
            "skipped": "no complete backups found; refused to delete anything",
        }

    complete.sort(
        key=lambda info: (info["latest_mtime"], info["bytes"], info["files"]),
        reverse=True,
    )
    kept_infos = complete[:keep_backups]
    kept_resolved = [pathlib.Path(info["path"]).resolve() for info in kept_infos]
    kept_paths = set(kept_resolved)
    kept_jsonl_keys: set[str] = set()
    for info in kept_infos:
        kept_jsonl_keys.update(jsonl_identity_keys(pathlib.Path(info["path"])))
    deleted: list[str] = []
    skipped: list[dict[str, Any]] = []

    for info in inspected:
        path = pathlib.Path(info["path"])
        resolved = path.resolve()
        if resolved in kept_paths:
            continue
        if resolved.parent != root_resolved:
            raise RuntimeError(f"Refusing to delete non-child backup path: {resolved}")
        missing_from_kept = jsonl_identity_keys(path) - kept_jsonl_keys
        if missing_from_kept:
            skipped.append(
                {
                    "path": str(resolved),
                    "reason": "retained backups do not cover all JSONL session ids/paths",
                    "missing_jsonl_keys": len(missing_from_kept),
                    "missing_jsonl_sample": sorted_sample(missing_from_kept),
                }
            )
            continue
        shutil.rmtree(resolved)
        deleted.append(str(resolved))

    return {
        "backup_root": str(backup_root),
        "keep_backups": keep_backups,
        "kept": [str(path) for path in kept_resolved],
        "deleted": deleted,
        "skipped": skipped,
        "inspected": inspected,
    }


def db_change_counts(path: pathlib.Path, provider: str, thread_id: str | None) -> dict[str, Any]:
    con = connect_ro(path)
    try:
        scope = " and id=?" if thread_id else ""
        params: tuple[Any, ...] = (provider, thread_id) if thread_id else (provider,)
        provider_rows = con.execute(
            "select count(*) from threads where (model_provider is null or model_provider<>?)" + scope,
            params,
        ).fetchone()[0]
        cwd_params: tuple[Any, ...] = (thread_id,) if thread_id else ()
        cwd_rows = con.execute(
            "select count(*) from threads where cwd like '\\\\?\\%'" + scope,
            cwd_params,
        ).fetchone()[0]
        exists = (
            con.execute("select count(*) from threads where id=?", (thread_id,)).fetchone()[0]
            if thread_id
            else con.execute("select count(*) from threads").fetchone()[0]
        )
        return {
            "path": str(path),
            "thread_rows": exists,
            "provider_rows": provider_rows,
            "cwd_rows": cwd_rows,
        }
    finally:
        con.close()


def apply_db(path: pathlib.Path, provider: str, thread_id: str | None = None) -> dict[str, Any]:
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout=30000")
    before_provider = {
        r["provider"]: r["n"]
        for r in con.execute(
            "select coalesce(model_provider,'<NULL>') provider, count(*) n "
            "from threads group by coalesce(model_provider,'<NULL>')"
        )
    }
    before_cwd = {
        r["kind"]: r["n"]
        for r in con.execute(
            "select case when cwd like '\\\\?\\%' then 'extended' "
            "when cwd is null then 'null' else 'normal' end kind, count(*) n "
            "from threads group by kind"
        )
    }
    scope = " and id=?" if thread_id else ""
    provider_params: tuple[Any, ...] = (provider, thread_id) if thread_id else (provider,)
    cwd_params: tuple[Any, ...] = (thread_id,) if thread_id else ()
    provider_rows = con.execute(
        "select count(*) from threads where (model_provider is null or model_provider<>?)" + scope,
        provider_params,
    ).fetchone()[0]
    cwd_rows = con.execute(
        "select count(*) from threads where cwd like '\\\\?\\%'" + scope,
        cwd_params,
    ).fetchone()[0]
    con.execute(
        "update threads set model_provider=? where (model_provider is null or model_provider<>?)" + scope,
        (provider, provider, thread_id) if thread_id else (provider, provider),
    )
    con.execute(
        "update threads set cwd=substr(cwd,5) where cwd like '\\\\?\\%'" + scope,
        cwd_params,
    )
    con.commit()
    after_provider = {
        r["provider"]: r["n"]
        for r in con.execute(
            "select coalesce(model_provider,'<NULL>') provider, count(*) n "
            "from threads group by coalesce(model_provider,'<NULL>')"
        )
    }
    after_cwd = {
        r["kind"]: r["n"]
        for r in con.execute(
            "select case when cwd like '\\\\?\\%' then 'extended' "
            "when cwd is null then 'null' else 'normal' end kind, count(*) n "
            "from threads group by kind"
        )
    }
    checkpoint = [tuple(r) for r in con.execute("pragma wal_checkpoint(passive)").fetchall()]
    con.close()
    return {
        "path": str(path),
        "thread_id": thread_id,
        "before_provider": before_provider,
        "before_cwd": before_cwd,
        "provider_rows_updated": provider_rows,
        "cwd_rows_updated": cwd_rows,
        "after_provider": after_provider,
        "after_cwd": after_cwd,
        "checkpoint": checkpoint,
    }


def verify_session_meta(
    path: pathlib.Path, provider: str, expected_thread_id: str | None = None
) -> None:
    first = first_json_line(path)
    payload = first.get("payload") if isinstance(first, dict) else None
    if first.get("type") != "session_meta" or not isinstance(payload, dict):
        raise RuntimeError(f"Missing session_meta after update: {path}")
    if expected_thread_id and payload.get("id") != expected_thread_id:
        raise RuntimeError(f"Thread id changed while updating {path}")
    if payload.get("model_provider") != provider:
        raise RuntimeError(f"Provider validation failed for {path}")
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.startswith(EXTENDED_PREFIX):
        raise RuntimeError(f"Extended cwd validation failed for {path}")


def rewrite_jsonl_file(
    path: pathlib.Path, provider: str, expected_thread_id: str | None = None
) -> tuple[bool, str]:
    record = read_first_json_record(path)
    if not record:
        return False, "none"
    item = record["item"]
    payload = item.get("payload") if isinstance(item, dict) else None
    if item.get("type") != "session_meta" or not isinstance(payload, dict):
        return False, "none"
    if expected_thread_id and payload.get("id") != expected_thread_id:
        raise RuntimeError(f"Refusing to update unexpected thread id in {path}")
    changed = False
    if payload.get("model_provider") != provider:
        payload["model_provider"] = provider
        changed = True
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.startswith(EXTENDED_PREFIX):
        payload["cwd"] = cwd[len(EXTENDED_PREFIX) :]
        changed = True
    if not changed:
        return False, "none"
    new_raw = (
        json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + record["newline"]
    )
    if len(new_raw) == len(record["raw"]):
        try:
            with path.open("r+b") as handle:
                handle.seek(record["offset"])
                handle.write(new_raw)
                handle.flush()
                os.fsync(handle.fileno())
            verify_session_meta(path, provider, expected_thread_id)
        except Exception:
            with path.open("r+b") as handle:
                handle.seek(record["offset"])
                handle.write(record["raw"])
                handle.flush()
                os.fsync(handle.fileno())
            raise
        return True, "in_place"

    tmp = path.with_name(
        f"{path.name}.codex-history-recovery-{os.getpid()}-{dt.datetime.now().strftime('%f')}.tmp"
    )
    try:
        with path.open("rb") as source, tmp.open("xb") as destination:
            prefix = source.read(record["offset"])
            if len(prefix) != record["offset"]:
                raise RuntimeError(f"File changed while preparing update: {path}")
            destination.write(prefix)
            destination.write(new_raw)
            source.seek(record["offset"] + len(record["raw"]))
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        shutil.copystat(path, tmp)
        verify_session_meta(tmp, provider, expected_thread_id)
        os.replace(tmp, path)
        verify_session_meta(path, provider, expected_thread_id)
        return True, "stream_replace"
    finally:
        if tmp.exists():
            tmp.unlink()


def apply_jsonl_changes(changes: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    files_changed = 0
    modes: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    for change in changes:
        path = change["path"]
        try:
            changed, mode = rewrite_jsonl_file(path, provider, change.get("thread_id"))
        except Exception as exc:
            errors.append({"path": str(path), "error": repr(exc)})
            continue
        if changed:
            files_changed += 1
            modes[mode] += 1
    return {
        "files_planned": len(changes),
        "files_changed": files_changed,
        "write_modes": dict(modes),
        "errors": errors,
    }


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def public_jsonl_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "files_seen": plan["files_seen"],
        "files_to_change": plan["files_to_change"],
        "changes": [
            {**change, "path": str(change["path"])} for change in plan["changes"]
        ],
        "errors": plan["errors"],
    }


def cmd_status(args: argparse.Namespace) -> int:
    codex_home = pathlib.Path(args.codex_home).expanduser()
    provider = load_current_provider(codex_home)
    roots = read_global_roots(codex_home)
    report = {
        "codex_home": str(codex_home),
        "current_provider": provider,
        "running_codex_processes": running_codex_processes(),
        "state_dbs": [
            summarize_db(path, roots, args.thread_id) for path in candidate_state_dbs(codex_home)
        ],
        "jsonl": summarize_jsonl(codex_home, args.thread_id),
    }
    print_json(report)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    codex_home = pathlib.Path(args.codex_home).expanduser()
    provider = args.provider or load_current_provider(codex_home)
    backup_root = (
        pathlib.Path(args.backup_root).expanduser()
        if args.backup_root
        else default_backup_root(codex_home)
    )
    db_plan = [
        db_change_counts(path, provider, args.thread_id)
        for path in candidate_state_dbs(codex_home)
    ]
    jsonl_plan = collect_jsonl_changes(codex_home, provider, args.thread_id)
    preflight = {
        "thread_id": args.thread_id,
        "db": db_plan,
        "jsonl": public_jsonl_plan(jsonl_plan),
    }
    if (
        args.thread_id
        and not any(item["thread_rows"] for item in db_plan)
        and jsonl_plan["files_seen"] == 0
    ):
        print_json(
            {
                "error": f"Thread id was not found: {args.thread_id}",
                "preflight": preflight,
                "backup_created": False,
            }
        )
        return 4
    if jsonl_plan["errors"]:
        print_json(
            {
                "error": "Preflight could not safely inspect every target JSONL file.",
                "preflight": preflight,
                "backup_created": False,
            }
        )
        return 3
    total_db_changes = sum(item["provider_rows"] + item["cwd_rows"] for item in db_plan)
    if total_db_changes == 0 and not jsonl_plan["changes"]:
        print_json(
            {
                "result": "no_changes",
                "target_provider": provider,
                "preflight": preflight,
                "backup_created": False,
            }
        )
        return 0

    running = running_codex_processes()
    if running and not args.allow_running:
        print_json(
            {
                "error": "Codex processes are running. Fully quit Codex Desktop before apply, or rerun with --allow-running.",
                "running_codex_processes": running,
                "preflight": preflight,
                "backup_created": False,
            }
        )
        return 2

    backup_mode = "full"
    increment: pathlib.Path | None = None
    if args.thread_id:
        baseline = latest_complete_backup(codex_home, backup_root)
        if baseline:
            backup = baseline
            increment, backup_integrity = make_incremental_backup(
                codex_home,
                baseline,
                args.thread_id,
                [change["path"] for change in jsonl_plan["changes"]],
            )
            backup_mode = "incremental"
        else:
            backup = make_backup(codex_home, backup_root)
            backup_integrity = backup_source_coverage(backup, codex_home)
            backup_mode = "full_baseline"
    else:
        backup = make_backup(codex_home, backup_root)
        backup_integrity = backup_source_coverage(backup, codex_home)

    if not backup_integrity.get("complete"):
        print_json(
            {
                "error": "Backup did not pass pre-write validation; no source data was changed.",
                "backup_dir": str(backup),
                "backup_increment": str(increment) if increment else None,
                "backup_integrity": backup_integrity,
            }
        )
        return 3

    db_reports = [
        apply_db(path, provider, args.thread_id) for path in candidate_state_dbs(codex_home)
    ]
    jsonl_report = apply_jsonl_changes(jsonl_plan["changes"], provider)
    remaining_db = [
        db_change_counts(path, provider, args.thread_id)
        for path in candidate_state_dbs(codex_home)
    ]
    remaining_jsonl = collect_jsonl_changes(codex_home, provider, args.thread_id)
    validation = {
        "ok": (
            not jsonl_report["errors"]
            and not remaining_jsonl["errors"]
            and remaining_jsonl["files_to_change"] == 0
            and all(
                item["provider_rows"] == 0 and item["cwd_rows"] == 0
                for item in remaining_db
            )
        ),
        "remaining_db": remaining_db,
        "remaining_jsonl": public_jsonl_plan(remaining_jsonl),
    }
    roots = read_global_roots(codex_home)
    final_status = [
        summarize_db(path, roots, args.thread_id) for path in candidate_state_dbs(codex_home)
    ]
    report = {
        "backup_dir": str(backup),
        "backup_increment": str(increment) if increment else None,
        "backup_mode": backup_mode,
        "backup_root": str(backup_root),
        "target_provider": provider,
        "preflight": preflight,
        "running_codex_processes": running,
        "db_apply": db_reports,
        "jsonl_apply": jsonl_report,
        "final_state_dbs": final_status,
        "backup_integrity": backup_integrity,
        "validation": validation,
    }
    report_path = (increment or backup) / "apply-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if validation["ok"] and backup_mode != "incremental":
        report["backup_retention"] = prune_history_backups(
            codex_home, args.keep_backups, backup_root
        )
    elif validation["ok"]:
        report["backup_retention"] = {
            "backup_root": str(backup_root),
            "keep_backups": args.keep_backups,
            "kept": [str(backup)],
            "deleted": [],
            "skipped": "targeted recovery reused the latest complete baseline",
        }
    else:
        report["backup_retention"] = {
            "skipped": "post-write validation failed; refused to prune any backups"
        }
    if backup.exists():
        stats = backup_tree_stats(backup)
        report["backup_summary"] = {
            "path": str(backup),
            "total_bytes": stats["bytes"],
            "total_gb": round(stats["bytes"] / (1024**3), 2),
            "files": stats["files"],
            "jsonl": stats["jsonl"],
            "sqlite": stats["sqlite"],
            "latest_time": stats["latest_time"],
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print_json(report)
    return 0 if validation["ok"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-home",
        default=str(default_codex_home()),
        help="Codex home directory, default CODEX_HOME or ~/.codex.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Read-only diagnostics.")
    status.add_argument("--thread-id", help="Specific thread id to inspect.")
    status.set_defaults(func=cmd_status)
    apply = sub.add_parser("apply", help="Backup and repair provider/cwd metadata.")
    apply.add_argument(
        "--thread-id",
        help="Repair only this thread and append a small pre-write snapshot to the latest complete backup.",
    )
    apply.add_argument("--provider", help="Override target provider. Defaults to config.toml.")
    apply.add_argument(
        "--allow-running",
        action="store_true",
        help="Apply even if Codex Desktop/app-server processes appear to be running.",
    )
    apply.add_argument(
        "--keep-backups",
        type=int,
        default=1,
        help="Keep this many latest complete codex-history-recovery backups after a successful apply.",
    )
    apply.add_argument(
        "--backup-root",
        help="Backup root. Defaults to CODEX_HISTORY_BACKUP_DIR or D:\\CodexBackups\\codex-history-recovery on Windows.",
    )
    apply.set_defaults(func=cmd_apply)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
