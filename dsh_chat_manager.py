#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 对话管理器

一个小巧的本地工具：查看并删除 DeepSeek Harness（DSH）的工作区与对话。
仅使用 Python 标准库，界面是本地网页（默认只监听 127.0.0.1）。

删除范围（重要）：
  * 对话：``<DSH_HOME>/sessions/<项目目录>/<session-id>/`` 下的会话日志，
    以及 ``<DSH_HOME>/storages/session_projcache/sessions/<session-id>.json``。
  * 工作区：``<DSH_HOME>/storages/workspace.json`` 中的工作区记录、它在
    ``global.workspaceIds`` 中的顺序项，以及它名下的全部对话（同上）。
  * 工作区所指向的磁盘文件夹不会被删除；附件仓库（内容寻址、被多个对话共享）
    也不会被删除。

安全策略：
  * 检测到 DSH 进程时默认锁定所有删除操作（可用 --force 强制解除，风险自负）。
  * 每次写 ``workspace.json`` 前自动备份到 ``~/.dsh-chat-manager/backups``。
  * 写入采用临时文件 + 原子替换，并在写入后回读校验；失败自动还原备份。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

APP_NAME = "DSH 对话管理器"
APP_VERSION = "1.0.0"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 1_000_000
SAFE_SEGMENT_CHARS = set(string.ascii_letters + string.digits + "._-")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


class ManagerError(Exception):
    """可展示给用户的错误。"""


class DshRunningError(ManagerError):
    """DSH 正在运行时拒绝删除。"""

    def __init__(self, status: "DshStatus") -> None:
        self.status = status
        names = "、".join(sorted({p.name for p in status.processes})) or "DSH"
        super().__init__(f"检测到 DSH 正在运行（{names}），请先退出 DSH 再删除。")


def default_home() -> Path:
    """DSH_HOME，缺省为 ``~/.dsh``。"""
    env = os.environ.get("DSH_HOME", "").strip()
    return Path(env) if env else Path.home() / ".dsh"


def default_backup_dir() -> Path:
    return Path.home() / ".dsh-chat-manager" / "backups"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ms_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _encode_unit(unit: int) -> str:
    return "~" + format(unit, "04X")


def encode_segment(raw: str) -> str:
    """复刻 DSH ``encodeSegment``：把任意字符串编码成单个安全路径段。

    与 DSH 的实现保持一致（含非 BMP 字符按 UTF-16 code unit 展开），
    因此可以用它反查会话目录。
    """
    if not raw:
        raise ValueError("cannot encode an empty path segment")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out: list[str] = []
    data = raw.encode("utf-16-le", "surrogatepass")
    for index in range(0, len(data), 2):
        unit = int.from_bytes(data[index : index + 2], "little")
        character = chr(unit)
        if character != "~" and character in SAFE_SEGMENT_CHARS:
            out.append(character)
        else:
            out.append(_encode_unit(unit))
    return "".join(out)


def decode_segment(encoded: str) -> str:
    """``encode_segment`` 的逆运算。"""
    if encoded == "~002E":
        return "."
    if encoded == "~002E~002E":
        return ".."
    units: list[int] = []
    index = 0
    while index < len(encoded):
        character = encoded[index]
        if character == "~" and index + 4 < len(encoded):
            chunk = encoded[index + 1 : index + 5]
            if all(c in string.hexdigits for c in chunk):
                units.append(int(chunk, 16))
                index += 5
                continue
        units.extend(ord(c) for c in character)
        index += 1
    raw = b"".join(unit.to_bytes(2, "little") for unit in units)
    return raw.decode("utf-16-le", "surrogatepass")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass
class SessionView:
    id: str
    title: str | None
    archived: bool
    pinned: bool
    cwd: str | None
    created_at_ms: int | None
    updated_at_ms: int | None
    has_log: bool
    has_cache: bool
    log_size: int
    log_dir: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "archived": self.archived,
            "pinned": self.pinned,
            "cwd": self.cwd,
            "createdAt": _ms_to_iso(self.created_at_ms),
            "updatedAt": _ms_to_iso(self.updated_at_ms),
            "hasLog": self.has_log,
            "hasCache": self.has_cache,
            "logSize": self.log_size,
        }


@dataclass
class WorkspaceView:
    id: str
    title: str
    path: str
    created_at: str | None
    updated_at: str | None
    sessions: list[SessionView]
    exists: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "exists": self.exists,
            "sessions": [session.to_dict() for session in self.sessions],
        }


@dataclass
class ManagerState:
    home: Path
    storages: Path
    workspace_file: Path
    sessions_root: Path
    cache_root: Path
    workspaces: list[WorkspaceView]
    file_mtime_ns: int
    file_size: int
    generated_at: float = field(default_factory=time.time)

    @property
    def sessions(self) -> list[SessionView]:
        return [session for workspace in self.workspaces for session in workspace.sessions]

    def stats(self) -> dict[str, int]:
        sessions = self.sessions
        return {
            "workspaces": len(self.workspaces),
            "sessions": len(sessions),
            "archived": sum(1 for session in sessions if session.archived),
            "missingLogs": sum(1 for session in sessions if not session.has_log),
            "missingCaches": sum(1 for session in sessions if not session.has_cache),
            "bytes": sum(session.log_size for session in sessions),
        }


# --------------------------------------------------------------------------- #
# 读取 DSH 数据
# --------------------------------------------------------------------------- #


def _read_json(path: Path, limit: int = 8 * 1024 * 1024) -> Any:
    try:
        if path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _read_zstd_text(path: Path, limit: int = 8 * 1024 * 1024) -> str | None:
    """尽力解压 DSH 的 zstd JSONL 会话日志；环境不支持时返回 None。"""
    try:
        if path.stat().st_size > limit:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        from compression import zstd  # Python 3.14+

        return zstd.decompress(raw).decode("utf-8", "replace")
    except Exception:
        pass
    try:
        import zstandard  # type: ignore[import-not-found]

        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=64 * 1024 * 1024).decode(
            "utf-8", "replace"
        )
    except Exception:
        return None


def _log_meta(path: Path) -> tuple[str | None, str | None, int | None]:
    """从会话日志中取标题、cwd、创建时间（仅在投影缓存缺失时兜底）。"""
    text = _read_zstd_text(path)
    if not text:
        return None, None, None
    title: str | None = None
    cwd: str | None = None
    created: int | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "session":
            cwd = event.get("cwd") or cwd
            value = event.get("createdAt")
            if isinstance(value, (int, float)):
                created = int(value)
        elif kind == "session/title":
            data = event.get("data")
            if isinstance(data, dict):
                candidate = data.get("title")
                if isinstance(candidate, str) and candidate.strip():
                    title = candidate.strip()
    return title, cwd, created


def _cache_info(path: Path) -> tuple[str | None, str | None, int | None]:
    """从 session_projcache 中取标题、cwd、创建时间。"""
    payload = _read_json(path, limit=4 * 1024 * 1024)
    if not isinstance(payload, dict):
        return None, None, None
    record = payload.get("record")
    if not isinstance(record, dict):
        return None, None, None
    title: str | None = None
    cwd: str | None = None
    created: int | None = None
    rows = record.get("rows")
    if isinstance(rows, dict):
        title_row = rows.get("title")
        if isinstance(title_row, dict):
            candidate = title_row.get("val")
            if isinstance(candidate, str) and candidate.strip():
                title = candidate.strip()
    identity = record.get("identity")
    if isinstance(identity, dict):
        cwd_value = identity.get("cwd")
        if isinstance(cwd_value, str) and cwd_value:
            cwd = cwd_value
        created_value = identity.get("createdAt")
        if isinstance(created_value, (int, float)):
            created = int(created_value)
    return title, cwd, created


def _dir_stats(path: Path) -> tuple[int, float, bool]:
    size = 0
    newest = 0.0
    has_log = False
    try:
        entries = list(path.rglob("*"))
    except OSError:
        return 0, 0.0, False
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            continue
        size += stat.st_size
        newest = max(newest, stat.st_mtime)
        if entry.name.startswith("session"):
            has_log = True
    return size, newest, has_log


def _index_session_dirs(root: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    try:
        groups = list(root.iterdir())
    except OSError:
        return index
    for group in groups:
        if not group.is_dir():
            continue
        try:
            children = list(group.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            try:
                session_id = decode_segment(child.name)
            except (ValueError, UnicodeDecodeError):
                continue
            index.setdefault(session_id, []).append(child)
    return index


def _index_cache_files(root: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return index
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() != ".json":
            continue
        try:
            session_id = decode_segment(entry.stem)
        except (ValueError, UnicodeDecodeError):
            continue
        index[session_id] = entry
    return index


def load_state(home: Path) -> ManagerState:
    """读取 ``workspace.json``、会话目录与投影缓存，构建界面模型。"""
    home = Path(home).expanduser()
    storages = home / "storages"
    workspace_file = storages / "workspace.json"
    sessions_root = home / "sessions"
    cache_root = storages / "session_projcache" / "sessions"

    if not workspace_file.is_file():
        raise ManagerError(
            "未找到 DSH 工作区数据文件：\n"
            f"  {workspace_file}\n"
            "请确认 DSH 已安装并至少启动过一次；也可以用 --home 指定 DSH_HOME。"
        )

    try:
        raw_text = workspace_file.read_text(encoding="utf-8-sig")
        payload = json.loads(raw_text)
        file_stat = workspace_file.stat()
    except (OSError, ValueError) as exc:
        raise ManagerError(f"无法读取 {workspace_file}：{exc}") from exc

    if not isinstance(payload, dict):
        raise ManagerError(f"{workspace_file} 的内容不是 JSON 对象，已停止操作。")

    global_state = payload.get("global")
    if not isinstance(global_state, dict):
        global_state = {}
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        tables = {}
    records = tables.get("workspaces")
    if not isinstance(records, dict):
        records = {}

    archived_ids = {item for item in global_state.get("archivedSessionIds") or [] if isinstance(item, str)}
    pinned_ids = {item for item in global_state.get("pinnedSessionIds") or [] if isinstance(item, str)}

    dir_index = _index_session_dirs(sessions_root)
    cache_index = _index_cache_files(cache_root)

    def build_session(session_id: str) -> SessionView:
        directories = dir_index.get(session_id, [])
        log_dir = directories[0] if directories else None
        cache_path = cache_index.get(session_id)
        has_log = False
        size = 0
        newest_mtime = 0.0
        if log_dir is not None:
            size, newest_mtime, has_log = _dir_stats(log_dir)
        cache_mtime = 0.0
        if cache_path is not None:
            try:
                cache_mtime = cache_path.stat().st_mtime
            except OSError:
                cache_mtime = 0.0

        title: str | None = None
        cwd: str | None = None
        created_ms: int | None = None
        if cache_path is not None:
            title, cwd, created_ms = _cache_info(cache_path)
        if (title is None or cwd is None) and log_dir is not None:
            log_file = _main_log_file(log_dir)
            if log_file is not None:
                log_title, log_cwd, log_created = _log_meta(log_file)
                title = title or log_title
                cwd = cwd or log_cwd
                created_ms = created_ms or log_created

        updated_ms: int | None = None
        moment = max(newest_mtime, cache_mtime)
        if moment > 0:
            updated_ms = int(moment * 1000)

        return SessionView(
            id=session_id,
            title=title,
            archived=session_id in archived_ids,
            pinned=session_id in pinned_ids and session_id not in archived_ids,
            cwd=cwd,
            created_at_ms=created_ms,
            updated_at_ms=updated_ms,
            has_log=has_log,
            has_cache=cache_path is not None,
            log_size=size,
            log_dir=str(log_dir) if log_dir is not None else None,
        )

    session_cache: dict[str, SessionView] = {}

    def session_view(session_id: str) -> SessionView:
        view = session_cache.get(session_id)
        if view is None:
            view = build_session(session_id)
            session_cache[session_id] = view
        return view

    workspace_ids = [item for item in global_state.get("workspaceIds") or [] if isinstance(item, str)]
    ordered_ids = [wid for wid in workspace_ids if wid in records]
    remaining = [wid for wid in records.keys() if wid not in ordered_ids]
    remaining.sort(key=lambda wid: str((records.get(wid) or {}).get("updatedAt") or ""), reverse=True)

    workspaces: list[WorkspaceView] = []
    for workspace_id in ordered_ids + remaining:
        record = records.get(workspace_id)
        if not isinstance(record, dict):
            continue
        path_value = record.get("path") if isinstance(record.get("path"), str) else ""
        title_value = record.get("title") if isinstance(record.get("title"), str) else ""
        if not title_value:
            title_value = Path(path_value).name if path_value else workspace_id
        session_ids: list[str] = []
        for item in record.get("sessionIds") or []:
            if isinstance(item, str) and item not in session_ids:
                session_ids.append(item)
        sessions = [session_view(session_id) for session_id in session_ids]
        exists = bool(path_value) and Path(path_value).is_dir()
        workspaces.append(
            WorkspaceView(
                id=workspace_id,
                title=title_value,
                path=path_value,
                created_at=record.get("createdAt") if isinstance(record.get("createdAt"), str) else None,
                updated_at=record.get("updatedAt") if isinstance(record.get("updatedAt"), str) else None,
                sessions=sessions,
                exists=exists,
            )
        )

    return ManagerState(
        home=home,
        storages=storages,
        workspace_file=workspace_file,
        sessions_root=sessions_root,
        cache_root=cache_root,
        workspaces=workspaces,
        file_mtime_ns=file_stat.st_mtime_ns,
        file_size=file_stat.st_size,
    )


def _main_log_file(log_dir: Path) -> Path | None:
    """优先取当前格式的日志，其次取任意 session*.jsonl* 文件。"""
    preferred = log_dir / "session.v4.jsonl.zstd"
    if preferred.is_file():
        return preferred
    candidates: list[Path] = []
    try:
        candidates = sorted(
            (child for child in log_dir.iterdir() if child.is_file() and child.name.startswith("session")),
            key=lambda child: child.name,
            reverse=True,
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------- #
# DSH 进程检测
# --------------------------------------------------------------------------- #


@dataclass
class DshProcess:
    pid: int
    name: str
    path: str | None = None
    command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "name": self.name, "path": self.path, "command": self.command}


@dataclass
class DshStatus:
    running: bool
    processes: list[DshProcess]
    source: str
    checked_at: float
    deep: bool = False
    note: str | None = None


_status_lock = threading.Lock()
_status_cache: dict[str, tuple[float, DshStatus]] = {}


def detect_dsh(*, deep: bool = False, refresh: bool = False) -> DshStatus:
    """检测 DSH 进程。结果缓存数秒；deep 模式额外检查 node 命令行。"""
    now = time.time()
    ttl = 20.0 if deep else 5.0
    with _status_lock:
        cached = _status_cache.get("deep" if deep else "fast")
        if cached is not None and not refresh and now - cached[0] < ttl:
            return cached[1]

    status = _collect_status(deep=deep)
    with _status_lock:
        _status_cache["deep" if deep else "fast"] = (now, status)
        if deep:
            _status_cache["fast"] = (now, status)
    return status


def remember_status(status: DshStatus) -> None:
    """把一次已经完成的检测结果放进缓存（启动时的深检测、测试注入等）。"""
    now = time.time()
    with _status_lock:
        _status_cache["fast"] = (now, status)
        if status.deep:
            _status_cache["deep"] = (now, status)


def _collect_status(*, deep: bool) -> DshStatus:
    processes: list[DshProcess] = []
    errors: list[str] = []
    if os.name == "nt":
        try:
            processes.extend(_scan_windows_processes())
        except Exception as exc:  # pragma: no cover - 防御性
            errors.append(f"进程扫描失败：{exc}")
        if deep:
            try:
                processes.extend(_scan_windows_node_processes())
            except Exception as exc:  # pragma: no cover - 防御性
                errors.append(f"命令行扫描失败：{exc}")
    else:
        try:
            processes.extend(_scan_posix_processes())
        except Exception as exc:  # pragma: no cover - 防御性
            errors.append(f"进程扫描失败：{exc}")

    deduped: dict[tuple[int, str], DshProcess] = {}
    for process in processes:
        deduped.setdefault((process.pid, process.name.lower()), process)
    unique = list(deduped.values())
    return DshStatus(
        running=bool(unique),
        processes=unique,
        source="process-scan" if unique else "none",
        checked_at=time.time(),
        deep=deep,
        note="；".join(errors) if errors else None,
    )


def _scan_windows_processes() -> list[DshProcess]:
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE or not snapshot:
        return []

    found: list[DshProcess] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            name = entry.szExeFile or ""
            pid = int(entry.th32ProcessID)
            if pid and _process_name_is_dsh(name):
                path = _windows_image_path(kernel32, pid, PROCESS_QUERY_LIMITED_INFORMATION)
                found.append(DshProcess(pid=pid, name=name, path=path))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _process_name_is_dsh(name: str, path: str | None = None) -> bool:
    lowered = (name or "").strip().lower()
    if lowered in {"deepseek harness.exe", "deepseek-harness.exe", "dsh.exe", "dsh"}:
        return True
    if "deepseek harness" in lowered or "deepseek-harness" in lowered:
        return True
    if path:
        path_lower = path.lower()
        if "deepseek harness" in path_lower or "deepseek-harness" in path_lower:
            return True
    return False


def _windows_image_path(kernel32: Any, pid: int, access: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
        return buffer.value if ok and buffer.value else None
    finally:
        kernel32.CloseHandle(handle)


def _scan_windows_node_processes() -> list[DshProcess]:
    """用 PowerShell 查 node/dsh 进程的命令行，捕捉 CLI 方式运行的 DSH。"""
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$p=@(Get-CimInstance Win32_Process -Filter \"Name='node.exe' OR Name='dsh.exe'\" | "
        "Select-Object ProcessId,Name,ExecutablePath,CommandLine);"
        "if($p.Count -gt 0){$p|ConvertTo-Json -Compress -Depth 3}"
    )
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            timeout=10,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    output = completed.stdout.decode("utf-8-sig", "replace").strip()
    if not output:
        return []
    try:
        payload = json.loads(output)
    except ValueError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []

    found: list[DshProcess] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        command = item.get("CommandLine") or ""
        if not isinstance(command, str) or not _command_is_dsh(command):
            continue
        try:
            pid = int(item.get("ProcessId") or 0)
        except (TypeError, ValueError):
            pid = 0
        found.append(
            DshProcess(
                pid=pid,
                name=str(item.get("Name") or "node.exe"),
                path=item.get("ExecutablePath") if isinstance(item.get("ExecutablePath"), str) else None,
                command=command,
            )
        )
    return found


def _command_is_dsh(command: str) -> bool:
    lowered = command.lower()
    if "@deepseek-ai\\dsh" in lowered or "@deepseek-ai/dsh" in lowered:
        return True
    if re.search(r"[\\/]dsh[\\/](?:lib|src|bin)[\\/]", lowered):
        return True
    return False


def _scan_posix_processes() -> list[DshProcess]:
    found: list[DshProcess] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if "deepseek harness" in command.lower() or _command_is_dsh(command):
            found.append(DshProcess(pid=int(entry.name), name="dsh", command=command))
    return found


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _remove_tree(path: Path) -> None:
    errors: list[str] = []

    def record(_function: Any, failed_path: Any, exc: Any) -> None:
        errors.append(f"{failed_path}: {exc}")

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=record)
    else:  # pragma: no cover - 兼容旧 Python
        shutil.rmtree(path, onerror=lambda function, failed_path, info: record(function, failed_path, info[1]))
    if errors:
        raise OSError("; ".join(errors[:5]))


def _tree_size(path: Path) -> int:
    size = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                size += child.stat().st_size
        except OSError:
            continue
    return size


def _session_dirs_for(state: ManagerState, session_id: str) -> list[Path]:
    try:
        encoded = encode_segment(session_id)
    except ValueError:
        return []
    directories: list[Path] = []
    try:
        groups = [child for child in state.sessions_root.iterdir() if child.is_dir()]
    except OSError:
        return directories
    for group in groups:
        candidate = group / encoded
        if candidate.is_dir() and _is_within(candidate, state.sessions_root):
            directories.append(candidate)
    return directories


def _delete_session_files(state: ManagerState, session_id: str) -> tuple[int, list[str]]:
    freed = 0
    errors: list[str] = []

    try:
        cache_name = encode_segment(session_id)
    except ValueError:
        cache_name = None

    for directory in _session_dirs_for(state, session_id):
        freed += _tree_size(directory)
        try:
            _remove_tree(directory)
        except OSError as exc:
            errors.append(f"{directory}: {exc}")

    if cache_name is not None:
        cache_file = state.cache_root / f"{cache_name}.json"
        if cache_file.is_file() and _is_within(cache_file, state.cache_root):
            try:
                freed += cache_file.stat().st_size
                cache_file.unlink()
            except OSError as exc:
                errors.append(f"{cache_file}: {exc}")
    return freed, errors


def _prune_empty_groups(state: ManagerState) -> int:
    removed = 0
    try:
        groups = [child for child in state.sessions_root.iterdir() if child.is_dir()]
    except OSError:
        return 0
    for group in groups:
        try:
            if not any(group.iterdir()):
                group.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


def _backup_workspace_file(workspace_file: Path, backup_dir: Path | None) -> Path:
    target_dir = (backup_dir or default_backup_dir()).expanduser()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"workspace-{stamp}-{os.getpid()}.json"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workspace_file, target)
    except OSError as exc:
        raise ManagerError(f"无法备份 workspace.json 到 {target_dir}：{exc}") from exc
    _prune_backups(target_dir, keep=60)
    return target


def _prune_backups(directory: Path, keep: int) -> None:
    try:
        backups = sorted(directory.glob("workspace-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in backups[keep:]:
        try:
            stale.unlink()
        except OSError:
            continue


def _apply_registry_changes(
    payload: dict[str, Any],
    deleted_workspace_ids: set[str],
    deleted_session_ids: set[str],
) -> bool:
    """原地修改 workspace.json 的结构，返回是否有改动。"""
    changed = False
    now_iso = _now_iso()

    global_state = payload.get("global")
    if not isinstance(global_state, dict):
        global_state = {}
        payload["global"] = global_state
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        tables = {}
        payload["tables"] = tables
    records = tables.get("workspaces")
    if not isinstance(records, dict):
        records = {}
        tables["workspaces"] = records

    for workspace_id in deleted_workspace_ids:
        if workspace_id in records:
            records.pop(workspace_id, None)
            changed = True

    workspace_ids = global_state.get("workspaceIds")
    if isinstance(workspace_ids, list):
        filtered = [item for item in workspace_ids if item not in deleted_workspace_ids]
        if filtered != workspace_ids:
            global_state["workspaceIds"] = filtered
            changed = True

    for key in ("archivedSessionIds", "pinnedSessionIds"):
        values = global_state.get(key)
        if isinstance(values, list):
            filtered = [item for item in values if item not in deleted_session_ids]
            if filtered != values:
                global_state[key] = filtered
                changed = True

    pending = global_state.get("pendingMutation")
    if isinstance(pending, dict) and pending.get("workspaceId") in deleted_workspace_ids:
        global_state.pop("pendingMutation", None)
        changed = True

    for workspace_id, record in records.items():
        if not isinstance(record, dict):
            continue
        session_ids = record.get("sessionIds")
        if not isinstance(session_ids, list):
            continue
        filtered = [item for item in session_ids if item not in deleted_session_ids]
        if filtered != session_ids:
            record["sessionIds"] = filtered
            record["updatedAt"] = now_iso
            changed = True

    return changed


def _write_workspace_file(state: ManagerState, payload: dict[str, Any], backup_file: Path) -> None:
    try:
        current = state.workspace_file.stat()
    except OSError as exc:
        raise ManagerError(f"无法读取 {state.workspace_file}：{exc}") from exc
    if (current.st_mtime_ns, current.st_size) != (state.file_mtime_ns, state.file_size):
        raise ManagerError("workspace.json 在读取后发生了变化（可能有其他程序正在写入），已取消本次删除。请刷新后重试。")

    temporary = state.workspace_file.with_name(
        f".{state.workspace_file.name}.{os.getpid()}.{random.randrange(16**6):06x}.tmp"
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state.workspace_file)
        json.loads(state.workspace_file.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        try:
            if backup_file.is_file():
                shutil.copy2(backup_file, state.workspace_file)
        finally:
            raise ManagerError(
                f"写入 workspace.json 失败（{exc}），已尝试用备份还原。请检查文件权限或磁盘状态。"
            ) from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def perform_deletion(
    home: Path,
    session_ids: Iterable[str],
    workspace_ids: Iterable[str],
    *,
    backup_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """执行删除，返回给界面/调用方的报告。"""
    state = load_state(home)
    workspaces_by_id = {workspace.id: workspace for workspace in state.workspaces}
    known_sessions = {session.id for workspace in state.workspaces for session in workspace.sessions}

    requested_workspaces = [item for item in dict.fromkeys(workspace_ids) if isinstance(item, str) and item]
    requested_sessions = [item for item in dict.fromkeys(session_ids) if isinstance(item, str) and item]

    missing_workspaces = [item for item in requested_workspaces if item not in workspaces_by_id]
    missing_sessions = [item for item in requested_sessions if item not in known_sessions]

    deleted_workspace_ids = [item for item in requested_workspaces if item in workspaces_by_id]
    target_sessions: set[str] = {item for item in requested_sessions if item in known_sessions}
    for workspace_id in deleted_workspace_ids:
        target_sessions.update(session.id for session in workspaces_by_id[workspace_id].sessions)

    report: dict[str, Any] = {
        "deletedWorkspaces": list(deleted_workspace_ids),
        "deletedSessions": sorted(target_sessions),
        "missingWorkspaces": missing_workspaces,
        "missingSessions": missing_sessions,
        "errors": [],
        "bytesFreed": 0,
        "backup": None,
        "removedEmptyGroups": 0,
    }

    if not deleted_workspace_ids and not target_sessions:
        return report

    if not force:
        status = detect_dsh(deep=True, refresh=True)
        if status.running:
            raise DshRunningError(status)

    payload = _read_json(state.workspace_file, limit=64 * 1024 * 1024)
    if not isinstance(payload, dict):
        raise ManagerError(f"{state.workspace_file} 无法解析，已停止操作。")

    changed = _apply_registry_changes(payload, set(deleted_workspace_ids), target_sessions)
    backup_file: Path | None = None
    if changed:
        backup_file = _backup_workspace_file(state.workspace_file, backup_dir)
        _write_workspace_file(state, payload, backup_file)
        report["backup"] = str(backup_file)

    for session_id in sorted(target_sessions):
        freed, errors = _delete_session_files(state, session_id)
        report["bytesFreed"] += freed
        report["errors"].extend(errors)

    report["removedEmptyGroups"] = _prune_empty_groups(state)
    return report


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #


def _static_dir() -> Path:
    return Path(__file__).resolve().parent / "web"


def _backup_summary(directory: Path) -> dict[str, Any]:
    try:
        backups = sorted(directory.glob("workspace-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        backups = []
    return {
        "dir": str(directory),
        "count": len(backups),
        "latest": backups[0].name if backups else None,
    }


def build_payload(state: ManagerState, status: DshStatus, *, backup_dir: Path, allow_force: bool) -> dict[str, Any]:
    return {
        "app": {"name": APP_NAME, "version": APP_VERSION},
        "home": str(state.home),
        "workspaceFile": str(state.workspace_file),
        "backupDir": str(backup_dir),
        "force": allow_force,
        "dsh": {
            "running": status.running,
            "checkedAt": _ms_to_iso(status.checked_at * 1000),
            "source": status.source,
            "processes": [process.to_dict() for process in status.processes],
            "note": status.note,
        },
        "stats": state.stats(),
        "workspaces": [workspace.to_dict() for workspace in state.workspaces],
        "backup": _backup_summary(backup_dir),
    }


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        home: Path,
        backup_dir: Path,
        allow_force: bool,
        initial_status: DshStatus | None = None,
    ) -> None:
        self.home = home
        self.backup_dir = backup_dir
        self.allow_force = allow_force
        self.static_dir = _static_dir()
        self.started_at = time.time()
        status = initial_status or detect_dsh(deep=True, refresh=True)
        super().__init__(address, handler)
        self.initial_status = status
        remember_status(status)

    def allowed_origins(self) -> set[str]:
        port = self.server_port
        return {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"DshChatManager/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    # -- 基础响应 ---------------------------------------------------------- #

    def _send_bytes(self, status: int, body: bytes, content_type: str, *, download_name: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str, *, code: str = "error", extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"ok": False, "error": code, "message": message}
        if extra:
            payload.update(extra)
        self._send_json(status, payload)

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return hostname in {"127.0.0.1", "localhost"}

    def log_message(self, format_string: str, *args: Any) -> None:  # noqa: A002 - 覆盖标准库签名
        stamp = datetime.now().strftime("%H:%M:%S")
        sys.stdout.write(f"[{stamp}] {self.address_string()} {format_string % args}\n")
        sys.stdout.flush()

    # -- 路由 -------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802 - 标准库约定
        if not self._host_allowed():
            self._error(HTTPStatus.FORBIDDEN, "非法的 Host 头。")
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
                return
            if path in ("/styles.css", "/app.js", "/favicon.svg"):
                self._serve_static(path.lstrip("/"))
                return
            if path == "/api/state":
                query = parse_qs(parsed.query)
                deep = (query.get("deep", ["0"])[0] or "").lower() in {"1", "true", "yes"}
                state = load_state(self.server.home)  # type: ignore[attr-defined]
                status = detect_dsh(deep=deep, refresh=deep)
                payload = build_payload(
                    state,
                    status,
                    backup_dir=self.server.backup_dir,  # type: ignore[attr-defined]
                    allow_force=self.server.allow_force,  # type: ignore[attr-defined]
                )
                self._send_json(HTTPStatus.OK, payload)
                return
            self._error(HTTPStatus.NOT_FOUND, "页面或接口不存在。", code="not-found")
        except ManagerError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), code="state-error")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover - 防御性
            self.log_message("state error: %s", exc)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"读取数据失败：{exc}")

    def do_POST(self) -> None:  # noqa: N802 - 标准库约定
        if not self._host_allowed():
            self._error(HTTPStatus.FORBIDDEN, "非法的 Host 头。")
            return
        if urlparse(self.path).path != "/api/delete":
            self._error(HTTPStatus.NOT_FOUND, "接口不存在。", code="not-found")
            return
        if (self.headers.get("X-Requested-With") or "") != "dsh-chat-manager":
            self._error(HTTPStatus.FORBIDDEN, "缺少请求标记，已拒绝。", code="forbidden")
            return
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins():  # type: ignore[attr-defined]
            self._error(HTTPStatus.FORBIDDEN, "请求来源不被允许。", code="forbidden")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "请求体为空。", code="bad-request")
            return
        if length > MAX_REQUEST_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体过大。", code="too-large")
            return

        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "请求体不是合法 JSON。", code="bad-request")
            return
        if not isinstance(body, dict):
            self._error(HTTPStatus.BAD_REQUEST, "请求体格式错误。", code="bad-request")
            return

        session_ids = [item for item in body.get("sessionIds") or [] if isinstance(item, str)]
        workspace_ids = [item for item in body.get("workspaceIds") or [] if isinstance(item, str)]
        if len(session_ids) > 5000 or len(workspace_ids) > 500:
            self._error(HTTPStatus.BAD_REQUEST, "一次删除的条目过多。", code="too-many")
            return
        if not session_ids and not workspace_ids:
            self._error(HTTPStatus.BAD_REQUEST, "没有指定要删除的对话或工作区。", code="empty")
            return

        try:
            report = perform_deletion(
                self.server.home,  # type: ignore[attr-defined]
                session_ids,
                workspace_ids,
                backup_dir=self.server.backup_dir,  # type: ignore[attr-defined]
                force=self.server.allow_force,  # type: ignore[attr-defined]
            )
            state = load_state(self.server.home)  # type: ignore[attr-defined]
            status = detect_dsh(deep=False)
            payload = build_payload(
                state,
                status,
                backup_dir=self.server.backup_dir,  # type: ignore[attr-defined]
                allow_force=self.server.allow_force,  # type: ignore[attr-defined]
            )
            self._send_json(HTTPStatus.OK, {"ok": True, "report": report, "state": payload})
        except DshRunningError as exc:
            self._error(
                HTTPStatus.CONFLICT,
                str(exc),
                code="dsh-running",
                extra={"processes": [process.to_dict() for process in exc.status.processes]},
            )
        except ManagerError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), code="delete-failed")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover - 防御性
            self.log_message("delete error: %s", exc)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"删除失败：{exc}")

    # -- 静态文件 ---------------------------------------------------------- #

    def _serve_static(self, name: str) -> None:
        base = self.server.static_dir  # type: ignore[attr-defined]
        target = (base / name).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "文件不存在。", code="not-found")
            return
        if not target.is_file():
            self._error(
                HTTPStatus.NOT_FOUND,
                "界面文件缺失。请确认 web/ 目录与 dsh_chat_manager.py 放在一起。",
                code="not-found",
            )
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        content_type = content_types.get(target.suffix.lower(), "application/octet-stream")
        self._send_bytes(HTTPStatus.OK, target.read_bytes(), content_type)


def build_server(
    home: Path,
    port: int,
    *,
    backup_dir: Path | None = None,
    allow_force: bool = False,
    initial_status: DshStatus | None = None,
) -> ManagerServer:
    server = ManagerServer(
        ("127.0.0.1", port),
        ApiHandler,
        home=home,
        backup_dir=backup_dir or default_backup_dir(),
        allow_force=allow_force,
        initial_status=initial_status,
    )
    return server


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #


def _print_status_line(status: DshStatus) -> str:
    if not status.running:
        return "DSH 未运行（可以安全删除）"
    names = sorted({process.name for process in status.processes})
    return f"DSH 正在运行：{'、'.join(names)}（共 {len(status.processes)} 个进程）"


def print_list(
    state: ManagerState,
    as_json: bool,
    *,
    backup_dir: Path | None = None,
    allow_force: bool = False,
) -> None:
    if as_json:
        status = detect_dsh(deep=True)
        payload = build_payload(
            state,
            status,
            backup_dir=backup_dir or default_backup_dir(),
            allow_force=allow_force,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    stats = state.stats()
    print(f"{APP_NAME} v{APP_VERSION}")
    print(f"数据目录 : {state.home}")
    print(f"工作区   : {stats['workspaces']}    对话 : {stats['sessions']}    日志 : {_human_size(stats['bytes'])}")
    print(f"DSH 状态 : {_print_status_line(detect_dsh(deep=True))}")
    print()
    for workspace in state.workspaces:
        print(f"● {workspace.title}  [{workspace.id}]")
        print(f"  {workspace.path or '(无路径)'}")
        if not workspace.sessions:
            print("    （无对话）")
        for session in workspace.sessions:
            flags: list[str] = []
            if session.archived:
                flags.append("已归档")
            if not session.has_log:
                flags.append("日志缺失")
            suffix = f"  [{' / '.join(flags)}]" if flags else ""
            size = _human_size(session.log_size) if session.has_log else "-"
            print(f"    - {session.title or '（无标题）'}")
            print(f"      {session.id}   {size}{suffix}")
        print()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dsh_chat_manager.py",
        description="DSH 对话管理器：查看并删除 DeepSeek Harness 的工作区与对话。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python dsh_chat_manager.py               启动本地网页界面\n"
            "  python dsh_chat_manager.py --list        在终端列出工作区与对话\n"
            "  python dsh_chat_manager.py --check       检查 DSH 是否在运行\n"
            "  python dsh_chat_manager.py --home D:\\dsh 管理另一份 DSH 数据\n"
        ),
    )
    parser.add_argument("--home", type=Path, default=None, help="DSH_HOME 目录（默认取环境变量 DSH_HOME，否则 ~/.dsh）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"网页端口（默认 {DEFAULT_PORT}，0 表示随机）")
    parser.add_argument("--backup-dir", type=Path, default=None, help="workspace.json 备份目录（默认 ~/.dsh-chat-manager/backups）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument(
        "--force",
        action="store_true",
        help="DSH 正在运行时仍允许删除（危险：可能导致数据被覆盖，强烈建议先退出 DSH）",
    )
    parser.add_argument("--list", action="store_true", help="在终端列出工作区与对话后退出")
    parser.add_argument("--check", action="store_true", help="只检查数据目录与 DSH 运行状态后退出")
    parser.add_argument("--json", action="store_true", help="配合 --list 输出 JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    home = (args.home or default_home()).expanduser()
    backup_dir = (args.backup_dir or default_backup_dir()).expanduser()

    try:
        state = load_state(home)
    except ManagerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.check:
        status = detect_dsh(deep=True, refresh=True)
        print(f"数据目录 : {state.home}")
        print(f"工作区文件 : {state.workspace_file}")
        print(f"备份目录 : {backup_dir}")
        print(f"DSH 状态 : {_print_status_line(status)}")
        if status.note:
            print(f"备注     : {status.note}")
        return 0

    if args.list:
        print_list(state, as_json=args.json, backup_dir=backup_dir, allow_force=args.force)
        return 0

    status = detect_dsh(deep=True, refresh=True)
    try:
        server = build_server(home, args.port, backup_dir=backup_dir, allow_force=args.force, initial_status=status)
    except OSError as exc:
        if args.port == 0:
            print(f"错误：无法监听本地端口：{exc}", file=sys.stderr)
            return 1
        print(f"端口 {args.port} 不可用（{exc}），改用随机端口。")
        server = build_server(home, 0, backup_dir=backup_dir, allow_force=args.force, initial_status=status)

    port = server.server_port
    url = f"http://127.0.0.1:{port}/"
    stats = state.stats()

    print()
    print(f"{APP_NAME} v{APP_VERSION}")
    print(f"数据目录 : {state.home}")
    print(f"工作区   : {stats['workspaces']} 个    对话 : {stats['sessions']} 个    日志 : {_human_size(stats['bytes'])}")
    print(f"备份目录 : {backup_dir}")
    print(f"DSH 状态 : {_print_status_line(status)}")
    if args.force:
        print("强制模式 : 已启用（DSH 运行时也允许删除，风险自负）")
    print(f"界面地址 : {url}")
    print("按 Ctrl+C 停止服务")
    print()

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:  # 输出被管道提前关闭（例如 | head）
        try:
            sys.stdout.close()
        except Exception:
            pass
        os._exit(0)
