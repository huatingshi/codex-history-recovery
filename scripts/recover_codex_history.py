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


def default_codex_home() -> pathlib.Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path.home() / ".codex"


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
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                return json.loads(line)
    return {}


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


def make_backup(codex_home: pathlib.Path) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = codex_home / "backups_state" / "codex-history-recovery" / stamp
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


def prune_history_backups(codex_home: pathlib.Path, keep_backups: int) -> dict[str, Any]:
    if keep_backups < 1:
        raise ValueError("--keep-backups must be at least 1")

    backup_root = codex_home / "backups_state" / "codex-history-recovery"
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


def apply_db(path: pathlib.Path, provider: str) -> dict[str, Any]:
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
    provider_rows = con.execute(
        "select count(*) from threads where model_provider is null or model_provider<>?",
        (provider,),
    ).fetchone()[0]
    cwd_rows = con.execute("select count(*) from threads where cwd like '\\\\?\\%'").fetchone()[0]
    con.execute(
        "update threads set model_provider=? where model_provider is null or model_provider<>?",
        (provider, provider),
    )
    con.execute("update threads set cwd=substr(cwd,5) where cwd like '\\\\?\\%'")
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
        "before_provider": before_provider,
        "before_cwd": before_cwd,
        "provider_rows_updated": provider_rows,
        "cwd_rows_updated": cwd_rows,
        "after_provider": after_provider,
        "after_cwd": after_cwd,
        "checkpoint": checkpoint,
    }


def rewrite_jsonl_file(path: pathlib.Path, provider: str) -> tuple[bool, int]:
    raw_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    changed = False
    lines_changed = 0
    for raw in raw_lines:
        if raw.endswith("\r\n"):
            body, newline = raw[:-2], "\r\n"
        elif raw.endswith("\n"):
            body, newline = raw[:-1], "\n"
        else:
            body, newline = raw, ""
        if not body.strip():
            out.append(raw)
            continue
        try:
            item = json.loads(body)
        except Exception:
            out.append(raw)
            continue
        payload = item.get("payload") if isinstance(item, dict) else None
        if item.get("type") == "session_meta" and isinstance(payload, dict):
            local_changed = False
            if isinstance(payload.get("model_provider"), str) and payload["model_provider"] != provider:
                payload["model_provider"] = provider
                local_changed = True
            if isinstance(payload.get("cwd"), str) and payload["cwd"].startswith(EXTENDED_PREFIX):
                payload["cwd"] = payload["cwd"][len(EXTENDED_PREFIX) :]
                local_changed = True
            if local_changed:
                changed = True
                lines_changed += 1
                out.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + newline)
                continue
        out.append(raw)
    if changed:
        tmp = path.with_name(path.name + ".codex-history-recovery-tmp")
        tmp.write_text("".join(out), encoding="utf-8", newline="")
        os.replace(tmp, path)
    return changed, lines_changed


def apply_jsonl(codex_home: pathlib.Path, provider: str) -> dict[str, Any]:
    files_seen = files_changed = lines_changed = 0
    errors: list[dict[str, str]] = []
    for dirname in ("sessions", "archived_sessions"):
        root = codex_home / dirname
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            files_seen += 1
            try:
                changed, n = rewrite_jsonl_file(path, provider)
            except Exception as exc:
                errors.append({"path": str(path), "error": repr(exc)})
                continue
            if changed:
                files_changed += 1
                lines_changed += n
    return {
        "files_seen": files_seen,
        "files_changed": files_changed,
        "lines_changed": lines_changed,
        "errors": errors,
    }


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


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
    running = running_codex_processes()
    if running and not args.allow_running:
        print_json(
            {
                "error": "Codex processes are running. Fully quit Codex Desktop before apply, or rerun with --allow-running.",
                "running_codex_processes": running,
            }
        )
        return 2
    backup = make_backup(codex_home)
    db_reports = [apply_db(path, provider) for path in candidate_state_dbs(codex_home)]
    jsonl_report = apply_jsonl(codex_home, provider)
    roots = read_global_roots(codex_home)
    final_status = [
        summarize_db(path, roots, args.thread_id) for path in candidate_state_dbs(codex_home)
    ]
    new_backup_integrity = backup_source_coverage(backup, codex_home)
    report = {
        "backup_dir": str(backup),
        "target_provider": provider,
        "running_codex_processes": running,
        "db_apply": db_reports,
        "jsonl_apply": jsonl_report,
        "final_state_dbs": final_status,
        "new_backup_integrity": new_backup_integrity,
    }
    (backup / "apply-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if new_backup_integrity["complete"]:
        report["backup_retention"] = prune_history_backups(codex_home, args.keep_backups)
    else:
        report["backup_retention"] = {
            "skipped": "new backup did not pass basic source coverage; refused to prune old backups"
        }
    if backup.exists():
        (backup / "apply-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print_json(report)
    return 0


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
    apply.add_argument("--thread-id", help="Specific thread id to include in final diagnostics.")
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
    apply.set_defaults(func=cmd_apply)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
