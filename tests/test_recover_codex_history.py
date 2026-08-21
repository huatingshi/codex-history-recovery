from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "recover_codex_history.py"
SPEC = importlib.util.spec_from_file_location("recover_codex_history", SCRIPT)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def write_db(path: pathlib.Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "create table threads ("
        "id text primary key, rollout_path text, model_provider text, cwd text, "
        "source text, thread_source text, archived integer, title text)"
    )
    con.executemany(
        "insert into threads values (:id,:rollout_path,:model_provider,:cwd,'cli','user',0,:title)",
        rows,
    )
    con.commit()
    con.close()


def write_jsonl(
    path: pathlib.Path,
    thread_id: str,
    provider: str,
    cwd: str,
    body: bytes = b'{"type":"response_item","payload":{"text":"message"}}\n',
) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "model_provider": provider,
            "cwd": cwd,
            "title": "unchanged",
        },
    }
    path.write_bytes(
        json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
        + body
    )
    return body


class RecoveryFixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.home = root / "codex-home"
        self.backups = root / "backups"
        self.home.mkdir()
        (self.home / "config.toml").write_text('model_provider = "newapi"\n', encoding="utf-8")
        (self.home / ".codex-global-state.json").write_text(
            json.dumps({"electron-saved-workspace-roots": ["D:\\Target", "D:\\Other"]}),
            encoding="utf-8",
        )

    def add_threads(self, provider: str = "openai", extended: bool = True) -> dict[str, pathlib.Path]:
        sessions = self.home / "sessions" / "2026" / "08" / "21"
        paths = {"target": sessions / "target.jsonl", "other": sessions / "other.jsonl"}
        prefix = recovery.EXTENDED_PREFIX if extended else ""
        rows = []
        for thread_id, cwd in (("target", "D:\\Target"), ("other", "D:\\Other")):
            path = paths[thread_id]
            write_jsonl(path, thread_id, provider, prefix + cwd)
            rows.append(
                {
                    "id": thread_id,
                    "rollout_path": str(path),
                    "model_provider": provider,
                    "cwd": prefix + cwd,
                    "title": thread_id,
                }
            )
        write_db(self.home / "state_5.sqlite", rows)
        return paths

    def args(self, thread_id: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            codex_home=str(self.home),
            provider=None,
            thread_id=thread_id,
            allow_running=True,
            keep_backups=1,
            backup_root=str(self.backups),
        )


def run_apply(fixture: RecoveryFixture, thread_id: str | None = None) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = recovery.cmd_apply(fixture.args(thread_id))
    return code, json.loads(output.getvalue())


class RecoveryTests(unittest.TestCase):
    def test_noop_skips_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RecoveryFixture(pathlib.Path(raw))
            fixture.add_threads(provider="newapi", extended=False)
            code, report = run_apply(fixture)
            self.assertEqual(code, 0)
            self.assertEqual(report["result"], "no_changes")
            self.assertFalse(report["backup_created"])
            self.assertFalse(fixture.backups.exists())

    def test_equal_length_provider_update_preserves_message_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            path = root / "thread.jsonl"
            body = b'{"type":"event","payload":{"binary":"AAECAw=="}}\n'
            write_jsonl(path, "target", "openai", "D:\\Target", body)
            before = path.read_bytes().split(b"\n", 1)[1]
            changed, mode = recovery.rewrite_jsonl_file(path, "newapi", "target")
            after = path.read_bytes().split(b"\n", 1)[1]
            self.assertTrue(changed)
            self.assertEqual(mode, "in_place")
            self.assertEqual(after, before)
            self.assertEqual(recovery.first_json_line(path)["payload"]["model_provider"], "newapi")

    def test_stream_replace_preserves_message_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            path = root / "thread.jsonl"
            body = b'{"type":"event","payload":{"text":"do not touch"}}\n'
            write_jsonl(path, "target", "openai", recovery.EXTENDED_PREFIX + "D:\\Target", body)
            before = path.read_bytes().split(b"\n", 1)[1]
            changed, mode = recovery.rewrite_jsonl_file(path, "newapi", "target")
            after = path.read_bytes().split(b"\n", 1)[1]
            self.assertTrue(changed)
            self.assertEqual(mode, "stream_replace")
            self.assertEqual(after, before)
            self.assertEqual(recovery.first_json_line(path)["payload"]["cwd"], "D:\\Target")

    def test_targeted_apply_reuses_baseline_and_only_changes_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RecoveryFixture(pathlib.Path(raw))
            paths = fixture.add_threads()
            baseline = recovery.make_backup(fixture.home, fixture.backups)
            (baseline / "apply-report.json").write_text("{}", encoding="utf-8")
            target_before = paths["target"].read_bytes()
            other_before = paths["other"].read_bytes()

            code, report = run_apply(fixture, "target")

            self.assertEqual(code, 0)
            self.assertTrue(report["validation"]["ok"])
            self.assertEqual(report["backup_mode"], "incremental")
            self.assertEqual(len([p for p in fixture.backups.iterdir() if p.is_dir()]), 1)
            increment = pathlib.Path(report["backup_increment"])
            backed_up_target = increment / paths["target"].relative_to(fixture.home)
            self.assertEqual(backed_up_target.read_bytes(), target_before)
            backed_up_db = increment / "state_5.sqlite"
            con = sqlite3.connect(backed_up_db)
            backed_up_provider = con.execute(
                "select model_provider from threads where id='target'"
            ).fetchone()[0]
            con.close()
            self.assertEqual(backed_up_provider, "openai")
            self.assertEqual(paths["other"].read_bytes(), other_before)
            self.assertEqual(recovery.first_json_line(paths["target"])["payload"]["model_provider"], "newapi")
            self.assertEqual(recovery.first_json_line(paths["other"])["payload"]["model_provider"], "openai")
            con = sqlite3.connect(fixture.home / "state_5.sqlite")
            rows = dict(con.execute("select id, model_provider from threads"))
            con.close()
            self.assertEqual(rows, {"target": "newapi", "other": "openai"})

    def test_full_apply_prunes_old_complete_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RecoveryFixture(pathlib.Path(raw))
            fixture.add_threads()
            old = recovery.make_backup(fixture.home, fixture.backups)
            (old / "apply-report.json").write_text("{}", encoding="utf-8")
            renamed = fixture.backups / "20000101-000000"
            old.rename(renamed)

            code, report = run_apply(fixture)

            self.assertEqual(code, 0)
            self.assertTrue(report["validation"]["ok"])
            directories = [path for path in fixture.backups.iterdir() if path.is_dir()]
            self.assertEqual(len(directories), 1)
            self.assertFalse(renamed.exists())
            self.assertEqual(report["backup_retention"]["deleted"], [str(renamed.resolve())])

    def test_preflight_error_never_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RecoveryFixture(pathlib.Path(raw))
            paths = fixture.add_threads()
            paths["target"].write_bytes(b"not-json\nmessage\n")

            code, report = run_apply(fixture, "target")

            self.assertEqual(code, 3)
            self.assertIn("Preflight", report["error"])
            self.assertFalse(report["backup_created"])
            self.assertFalse(fixture.backups.exists())

    def test_write_failure_keeps_backup_and_reports_failed_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RecoveryFixture(pathlib.Path(raw))
            fixture.add_threads()
            baseline = recovery.make_backup(fixture.home, fixture.backups)
            (baseline / "apply-report.json").write_text("{}", encoding="utf-8")

            with mock.patch.object(
                recovery, "rewrite_jsonl_file", side_effect=PermissionError("locked")
            ):
                code, report = run_apply(fixture, "target")

            self.assertEqual(code, 3)
            self.assertFalse(report["validation"]["ok"])
            self.assertIn("refused", report["backup_retention"]["skipped"])
            self.assertEqual(len([p for p in fixture.backups.iterdir() if p.is_dir()]), 1)
            self.assertTrue(pathlib.Path(report["backup_increment"]).exists())

    def test_unknown_thread_is_an_error_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RecoveryFixture(pathlib.Path(raw))
            fixture.add_threads()

            code, report = run_apply(fixture, "missing")

            self.assertEqual(code, 4)
            self.assertIn("not found", report["error"])
            self.assertFalse(report["backup_created"])
            self.assertFalse(fixture.backups.exists())


if __name__ == "__main__":
    unittest.main()
