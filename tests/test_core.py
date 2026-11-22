# -*- coding: utf-8 -*-
"""dsh_chat_manager 的单元测试。

全部测试都跑在临时目录里的假 DSH_HOME 上，不会触碰真实的 ~/.dsh。
运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

import dsh_chat_manager as manager

IDLE = manager.DshStatus(running=False, processes=[], source="test", checked_at=0.0)
RUNNING = manager.DshStatus(
    running=True,
    processes=[manager.DshProcess(pid=4242, name="DeepSeek Harness.exe")],
    source="test",
    checked_at=0.0,
)

HAS_ZSTD = True
try:  # Python 3.14+
    import compression.zstd  # noqa: F401
except Exception:  # pragma: no cover - 老版本 Python
    try:
        import zstandard  # noqa: F401
    except Exception:
        HAS_ZSTD = False


def make_case_dir() -> Path:
    """在系统临时目录里建一个普通权限的目录。

    不使用 ``tempfile.mkdtemp``：它创建的 0o700 目录在部分受限环境（例如
    DSH 的沙箱子进程）里不可再写入，会让测试本身失真。
    """
    base = Path(tempfile.gettempdir()) / "dsh-chat-manager-tests"
    base.mkdir(parents=True, exist_ok=True)
    target = base / uuid.uuid4().hex
    target.mkdir()
    return target


def write_cache(cache_dir: Path, session_id: str, title: str, cwd: Path, created: int = 1_795_000_000_000) -> None:
    payload = {
        "version": 7,
        "record": {
            "identity": {
                "formatVersion": 4,
                "createdAt": created,
                "cwd": str(cwd),
                "isSeeded": False,
                "inheritedEventCount": 0,
            },
            "rows": {"title": {"ver": 1, "seq": 5, "val": title}},
        },
    }
    target = cache_dir / f"{session_id}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_zstd_log(path: Path, session_id: str, title: str, cwd: Path) -> None:
    lines = [
        json.dumps(
            {
                "type": "session",
                "version": 4,
                "id": session_id,
                "createdAt": 1_795_000_000_000,
                "cwd": str(cwd),
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "type": "session/title",
                "seq": 1,
                "time": 1_795_000_000_001,
                "data": {"title": title},
            },
            ensure_ascii=False,
        ),
    ]
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    if HAS_ZSTD:
        try:
            from compression import zstd

            path.write_bytes(zstd.compress(raw))
            return
        except Exception:  # pragma: no cover - 依赖可选
            import zstandard

            path.write_bytes(zstandard.ZstdCompressor().compress(raw))
            return
    path.write_bytes(b"")


class Fixture:
    """一份可复现的假 DSH 数据。"""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "dsh-home"
        self.sessions = self.home / "sessions"
        self.cache = self.home / "storages" / "session_projcache" / "sessions"
        self.workspace_file = self.home / "storages" / "workspace.json"
        self.backup_dir = base / "backups"
        self.alpha = base / "projects" / "alpha"
        self.beta = base / "projects" / "beta"
        self.sessions.mkdir(parents=True)
        self.cache.mkdir(parents=True)
        self.alpha.mkdir(parents=True)
        self.beta.mkdir(parents=True)

        self.group = self.sessions / "--fixture-alpha--"
        for session_id in ("session-aaa", "session-bbb", "session-ddd"):
            (self.group / session_id).mkdir(parents=True)
        (self.group / "session-aaa" / "session.v4.jsonl.zstd").write_bytes(b"legacy-bytes")
        write_zstd_log(self.group / "session-ddd" / "session.v4.jsonl.zstd", "session-ddd", "日志里的标题", self.alpha)

        write_cache(self.cache, "session-aaa", "分析报告", self.alpha)
        write_cache(self.cache, "session-bbb", "第二个对话", self.alpha)
        write_cache(self.cache, "session-ccc", "第三个对话", self.beta)

        self.raw = {
            "unit": {"name": "workspace", "version": 2},
            "global": {
                "initialized": True,
                "workspaceIds": ["ws-alpha", "ws-beta"],
                "archivedSessionIds": ["session-bbb"],
                "pinnedSessionIds": ["session-aaa", "session-ddd"],
                "pendingMutation": {"operation": "delete", "workspaceId": "ws-beta"},
            },
            "tables": {
                "workspaces": {
                    "ws-alpha": {
                        "path": str(self.alpha),
                        "title": "Alpha 项目",
                        "sessionIds": ["session-aaa", "session-bbb", "session-ddd"],
                        "createdAt": "2026-01-01T00:00:00.000Z",
                        "updatedAt": "2026-01-02T00:00:00.000Z",
                    },
                    "ws-beta": {
                        "path": str(self.beta),
                        "title": "Beta 项目",
                        "sessionIds": ["session-ccc"],
                        "createdAt": "2026-01-03T00:00:00.000Z",
                        "updatedAt": "2026-01-04T00:00:00.000Z",
                    },
                }
            },
        }
        self.workspace_file.write_text(json.dumps(self.raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def registry(self) -> dict:
        return json.loads(self.workspace_file.read_text(encoding="utf-8"))


class ManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        manager._status_cache.clear()
        self.base = make_case_dir()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.fixture = Fixture(self.base)

    def state(self):
        return manager.load_state(self.fixture.home)

    def delete(self, session_ids=(), workspace_ids=(), *, force=False):
        return manager.perform_deletion(
            self.fixture.home,
            list(session_ids),
            list(workspace_ids),
            backup_dir=self.fixture.backup_dir,
            force=force,
        )


class SegmentTests(unittest.TestCase):
    def test_round_trip(self):
        for raw in ("session-abc", "a b~c", "中文目录", "emoji-😀-x", "dot.name"):
            encoded = manager.encode_segment(raw)
            self.assertEqual(manager.decode_segment(encoded), raw)

    def test_safe_ids_are_unchanged(self):
        self.assertEqual(manager.encode_segment("session-ff5525c4-6cc4"), "session-ff5525c4-6cc4")

    def test_special_characters_are_escaped(self):
        self.assertEqual(manager.encode_segment("a b"), "a~0020b")
        self.assertEqual(manager.encode_segment(".."), "~002E~002E")


class StateTests(ManagerTestCase):
    def test_lists_workspaces_in_registry_order(self):
        state = self.state()
        self.assertEqual([workspace.title for workspace in state.workspaces], ["Alpha 项目", "Beta 项目"])
        self.assertEqual([workspace.id for workspace in state.workspaces], ["ws-alpha", "ws-beta"])

    def test_session_titles_and_flags(self):
        state = self.state()
        sessions = {session.id: session for session in state.sessions}
        self.assertEqual(sessions["session-aaa"].title, "分析报告")
        self.assertTrue(sessions["session-bbb"].archived)
        self.assertFalse(sessions["session-bbb"].has_log)
        self.assertTrue(sessions["session-bbb"].has_cache)
        self.assertEqual(sessions["session-ccc"].title, "第三个对话")
        self.assertFalse(sessions["session-ccc"].has_log)

    @unittest.skipUnless(HAS_ZSTD, "需要 zstandard 支持")
    def test_title_falls_back_to_session_log(self):
        state = self.state()
        sessions = {session.id: session for session in state.sessions}
        self.assertEqual(sessions["session-ddd"].title, "日志里的标题")
        self.assertTrue(sessions["session-ddd"].has_log)
        self.assertFalse(sessions["session-ddd"].has_cache)

    def test_stats(self):
        stats = self.state().stats()
        self.assertEqual(stats["workspaces"], 2)
        self.assertEqual(stats["sessions"], 4)
        self.assertEqual(stats["archived"], 1)
        self.assertEqual(stats["missingLogs"], 2)

    def test_missing_home_raises_manager_error(self):
        with self.assertRaises(manager.ManagerError):
            manager.load_state(self.base / "absent")


class DeleteTests(ManagerTestCase):
    def test_delete_single_session(self):
        original = self.fixture.workspace_file.read_bytes()
        with mock.patch.object(manager, "detect_dsh", return_value=IDLE):
            report = self.delete(["session-aaa"])

        self.assertEqual(report["deletedSessions"], ["session-aaa"])
        self.assertEqual(report["deletedWorkspaces"], [])
        self.assertFalse(report["errors"])

        registry = self.fixture.registry()
        self.assertEqual(registry["tables"]["workspaces"]["ws-alpha"]["sessionIds"], ["session-bbb", "session-ddd"])
        self.assertNotIn("session-aaa", registry["global"]["pinnedSessionIds"])
        self.assertIn("session-bbb", registry["global"]["archivedSessionIds"])
        self.assertNotEqual(registry["tables"]["workspaces"]["ws-alpha"]["updatedAt"], "2026-01-02T00:00:00.000Z")

        self.assertFalse((self.fixture.group / "session-aaa").exists())
        self.assertTrue((self.fixture.group / "session-bbb").exists())
        self.assertFalse((self.fixture.cache / "session-aaa.json").exists())
        self.assertTrue((self.fixture.cache / "session-bbb.json").exists())

        backup = Path(report["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), original)

    def test_delete_workspace_removes_sessions_but_keeps_folder(self):
        with mock.patch.object(manager, "detect_dsh", return_value=IDLE):
            report = self.delete(workspace_ids=["ws-beta"])

        self.assertEqual(report["deletedWorkspaces"], ["ws-beta"])
        self.assertEqual(report["deletedSessions"], ["session-ccc"])
        registry = self.fixture.registry()
        self.assertNotIn("ws-beta", registry["tables"]["workspaces"])
        self.assertEqual(registry["global"]["workspaceIds"], ["ws-alpha"])
        self.assertNotIn("pendingMutation", registry["global"])
        self.assertFalse((self.fixture.cache / "session-ccc.json").exists())
        self.assertTrue(self.fixture.beta.is_dir())

    def test_delete_workspace_handles_sessions_without_files(self):
        with mock.patch.object(manager, "detect_dsh", return_value=IDLE):
            report = self.delete(workspace_ids=["ws-alpha"])

        registry = self.fixture.registry()
        self.assertNotIn("ws-alpha", registry["tables"]["workspaces"])
        self.assertEqual(registry["global"]["workspaceIds"], ["ws-beta"])
        self.assertTrue(self.fixture.alpha.is_dir())
        self.assertEqual(sorted(report["deletedSessions"]), ["session-aaa", "session-bbb", "session-ddd"])
        self.assertFalse(self.fixture.group.exists())

    def test_dsh_running_blocks_deletion(self):
        with mock.patch.object(manager, "detect_dsh", return_value=RUNNING):
            with self.assertRaises(manager.DshRunningError):
                self.delete(["session-aaa"])
        self.assertTrue((self.fixture.group / "session-aaa").is_dir())
        self.assertTrue((self.fixture.cache / "session-aaa.json").is_file())
        self.assertEqual(self.fixture.registry(), self.fixture.raw)

    def test_force_allows_deletion_while_running(self):
        with mock.patch.object(manager, "detect_dsh", return_value=RUNNING):
            report = self.delete(["session-aaa"], force=True)
        self.assertEqual(report["deletedSessions"], ["session-aaa"])
        self.assertFalse((self.fixture.group / "session-aaa").exists())

    def test_unknown_ids_are_reported_without_changes(self):
        with mock.patch.object(manager, "detect_dsh", return_value=IDLE):
            report = self.delete(["session-zzz"], ["ws-zzz"])
        self.assertEqual(report["missingSessions"], ["session-zzz"])
        self.assertEqual(report["missingWorkspaces"], ["ws-zzz"])
        self.assertEqual(report["deletedSessions"], [])
        self.assertIsNone(report["backup"])
        self.assertEqual(self.fixture.registry(), self.fixture.raw)

    def test_empty_request_is_noop(self):
        with mock.patch.object(manager, "detect_dsh", return_value=IDLE):
            report = self.delete()
        self.assertEqual(report["deletedSessions"], [])
        self.assertIsNone(report["backup"])

    def test_registry_is_rewritten_atomically_and_parsable(self):
        with mock.patch.object(manager, "detect_dsh", return_value=IDLE):
            self.delete(["session-bbb"])
        text = self.fixture.workspace_file.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertIsInstance(json.loads(text), dict)
        leftovers = list(self.fixture.workspace_file.parent.glob(".workspace.json.*.tmp"))
        self.assertEqual(leftovers, [])


class HttpTests(ManagerTestCase):
    def start_server(self, *, allow_force: bool, initial_status=IDLE):
        server = manager.build_server(
            self.fixture.home,
            0,
            backup_dir=self.fixture.backup_dir,
            allow_force=allow_force,
            initial_status=initial_status,
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, *, method="GET", body=None, headers=None):
        url = f"http://127.0.0.1:{server.server_port}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()

    def test_state_endpoint(self):
        server = self.start_server(allow_force=False)
        status, body = self.request(server, "/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["stats"]["workspaces"], 2)
        self.assertEqual(len(payload["workspaces"]), 2)
        self.assertFalse(payload["dsh"]["running"])

    def test_index_page_is_served(self):
        server = self.start_server(allow_force=False)
        status, body = self.request(server, "/")
        self.assertEqual(status, 200)
        self.assertIn("DSH 对话管理器".encode("utf-8"), body)
        status, body = self.request(server, "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"fetchState", body)

    def test_delete_requires_request_marker(self):
        server = self.start_server(allow_force=True)
        status, body = self.request(server, "/api/delete", method="POST", body={"sessionIds": ["session-aaa"]})
        self.assertEqual(status, 403)
        self.assertTrue((self.fixture.group / "session-aaa").is_dir())

    def test_delete_via_api(self):
        server = self.start_server(allow_force=True)
        status, body = self.request(
            server,
            "/api/delete",
            method="POST",
            body={"workspaceIds": ["ws-beta"]},
            headers={"X-Requested-With": "dsh-chat-manager"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["report"]["deletedWorkspaces"], ["ws-beta"])
        self.assertNotIn("ws-beta", self.fixture.registry()["tables"]["workspaces"])

    def test_delete_blocked_when_dsh_runs(self):
        server = self.start_server(allow_force=False)
        with mock.patch.object(manager, "detect_dsh", return_value=RUNNING):
            status, body = self.request(
                server,
                "/api/delete",
                method="POST",
                body={"sessionIds": ["session-aaa"]},
                headers={"X-Requested-With": "dsh-chat-manager"},
            )
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "dsh-running")
        self.assertTrue((self.fixture.group / "session-aaa").is_dir())


if __name__ == "__main__":
    unittest.main()
