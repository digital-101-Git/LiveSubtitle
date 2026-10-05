"""Destructive log deletion stays confined and respects queued history writes."""
import asyncio
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from engine.history import CaptionHistory
from engine.log_files import delete_log_files, log_directory, regular_log_path
from engine.server import create_app
from engine.sessions import SessionManager
from engine.tests.test_engine import FakeRuntime
from engine.tests.test_history import caption


def test_deletes_log_files_and_memory_without_touching_other_directories(tmp_path):
    history = CaptionHistory(tmp_path)
    history.upsert(caption(1))
    for name in ("llama.log", "caption-history.json.corrupt", ".caption-history-old.tmp"):
        (tmp_path / "logs" / name).write_text("old log", encoding="utf-8")
    keep = [tmp_path / "config/settings.json", tmp_path / "models/tiny.gguf",
            tmp_path / "logs/nested/untouched.txt", tmp_path / "notes.txt"]
    for path in keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")
    result = history.clear_logs(threading.RLock())
    assert result == {"ok": True, "deleted_count": 4, "failed_files": []}
    assert not history.path.exists() and history.snapshot() == []
    assert all(path.read_text(encoding="utf-8") == "keep" for path in keep)
    assert not history.upsert(caption(1, revision=2, is_final=True))
    assert not history.path.exists()  # Late revision cannot revive removed history.
    assert history.upsert(caption(2))
    assert [row["segment_id"] for row in CaptionHistory(tmp_path).snapshot()] == [2]


def test_missing_log_directory_is_idempotent_without_creating_a_file(tmp_path):
    history = CaptionHistory(tmp_path)
    for _ in range(2):
        assert history.clear_logs(threading.RLock()) == {"ok": True, "deleted_count": 0, "failed_files": []}
    assert not (tmp_path / "logs").exists()


def test_locked_file_reports_partial_failure_and_does_not_restore_old_history(tmp_path, monkeypatch):
    history = CaptionHistory(tmp_path)
    history.upsert(caption(1))
    blocked = tmp_path / "logs/llama.log"
    blocked.write_text("locked", encoding="utf-8")
    unlink = Path.unlink
    def guarded(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("locked by another process")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", guarded)
    assert history.clear_logs(threading.RLock()) == {"ok": False, "deleted_count": 1, "failed_files": ["llama.log"]}
    assert blocked.read_text(encoding="utf-8") == "locked"
    assert history.snapshot() == []
    history.upsert(caption(2))
    assert len(history.snapshot()) == 1


def test_linked_log_file_is_not_followed_or_written(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    directory = log_directory(tmp_path, create=True)
    linked = directory / "llama.log"
    linked.hardlink_to(outside)
    assert delete_log_files(tmp_path) == {"ok": False, "deleted_count": 0, "failed_files": ["llama.log"]}
    with pytest.raises(OSError):
        regular_log_path(directory, "llama.log")
    assert outside.read_text(encoding="utf-8") == "keep"


def test_log_directory_symlink_is_not_traversed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "llama.log").write_text("keep", encoding="utf-8")
    try:
        (tmp_path / "logs").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(OSError):
        delete_log_files(tmp_path)
    assert (outside / "llama.log").read_text(encoding="utf-8") == "keep"


def test_delete_api_auth_and_origin_and_keeps_active_models(tmp_path):
    runtime = FakeRuntime()
    runtime.log_lock = threading.RLock()
    app = create_app(tmp_path, runtime)
    history = app.state.sessions.history
    history.upsert(caption(1))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + app.state.settings.token}
        assert client.post("/v1/logs/clear").status_code == 401
        assert client.post("/v1/logs/clear", headers={**headers, "Origin": "chrome-extension://" + "a" * 32}).status_code == 403
        assert history.path.exists()
        with client.websocket_connect("/v1/stream") as ws:
            ws.send_json({"type": "auth", "token": app.state.settings.token})
            ws.send_json({"type": "start"})
            assert ws.receive_json()["type"] == "status"
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            session = app.state.sessions.active
            response = client.post("/v1/logs/clear", headers=headers, json={})
            assert response.status_code == 200 and response.json()["ok"]
            assert response.json()["deleted_count"] == 1
            assert app.state.sessions.active is session and session.active
            assert runtime.ready and runtime.prepared == 1
            assert not history.path.exists()
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "stopped"


@pytest.mark.asyncio
async def test_clear_is_between_old_and_new_queued_caption_writes(tmp_path, monkeypatch):
    history = CaptionHistory(tmp_path)
    manager = SessionManager(history)
    session = SimpleNamespace(_history_warning_sent=False)
    entered, release = threading.Event(), threading.Event()
    upsert = history.upsert
    def delayed(event):
        if event["segment_id"] == 1:
            entered.set()
            assert release.wait(5)
        return upsert(event)
    monkeypatch.setattr(history, "upsert", delayed)
    manager.record_caption(session, {"type": "caption", **caption(1)})
    assert await asyncio.to_thread(entered.wait, 2)
    deletion = asyncio.create_task(manager.clear_logs(threading.RLock()))
    await asyncio.sleep(0)  # clear_logs installs its ordered barrier before waiting.
    assert manager._history_task.get_name() == "clear-caption-logs"
    manager.record_caption(session, {"type": "caption", **caption(2)})
    release.set()
    assert (await asyncio.wait_for(deletion, 3))["ok"]
    await manager.flush_history()
    assert [row["segment_id"] for row in history.snapshot()] == [2]
    assert [row["segment_id"] for row in json.loads(history.path.read_text(encoding="utf-8"))] == [2]


@pytest.mark.asyncio
async def test_failed_clear_barrier_does_not_break_subsequent_caption_logging(tmp_path, monkeypatch):
    history = CaptionHistory(tmp_path)
    manager = SessionManager(history)
    def denied(lock):
        raise PermissionError("denied")
    monkeypatch.setattr(history, "clear_logs", denied)
    assert not (await manager.clear_logs(threading.RLock()))["ok"]
    manager.record_caption(SimpleNamespace(_history_warning_sent=False), {"type": "caption", **caption(2)})
    await manager.flush_history()
    assert history.snapshot()[0]["segment_id"] == 2
