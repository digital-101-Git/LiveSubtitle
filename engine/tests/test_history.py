import json
import errno

import pytest

from engine.history import CaptionHistory


def caption(segment_id, **changes):
    return {"session_id": "session-one", "segment_id": segment_id,
            "source_text": f"source {segment_id}", "text": f"번역 {segment_id}",
            "language": "en", "is_final": False, "revision": 1, **changes}


def test_rotation_keeps_latest_100_and_ignores_late_evicted_revision(tmp_path):
    history = CaptionHistory(tmp_path)
    for segment in range(1, 106):
        assert history.upsert(caption(segment))
    rows = json.loads(history.path.read_text(encoding="utf-8"))
    assert len(rows) == 100
    assert [row["segment_id"] for row in rows] == list(range(105, 5, -1))
    assert not history.upsert(caption(1, text="늦은 교정", revision=2, is_final=True))
    assert history.snapshot() == rows


def test_restart_continues_file_rotation_without_changing_existing_order(tmp_path):
    first = CaptionHistory(tmp_path, limit=3)
    for segment in range(1, 4):
        first.upsert(caption(segment))
    restarted = CaptionHistory(tmp_path, limit=3)
    assert restarted.snapshot() == first.snapshot()
    restarted.upsert(caption(1, session_id="new-session"))
    assert [(row["session_id"], row["segment_id"]) for row in restarted.snapshot()] == [
        ("new-session", 1), ("session-one", 3), ("session-one", 2)]


def test_correction_keeps_position_and_timestamp_and_snapshots_are_independent(tmp_path):
    history = CaptionHistory(tmp_path)
    history.upsert(caption(4))
    history.upsert(caption(1))  # Arrival order may differ from segment ID order.
    before = history.snapshot()
    history.upsert(caption(4, text="교정 번역", revision=2, is_final=True, translation_status="failed"))
    after = history.snapshot()
    assert [row["segment_id"] for row in after] == [1, 4]
    assert after[1]["timestamp"] == before[1]["timestamp"]
    assert after[1]["text"] == "교정 번역" and after[1]["status"] == "failed"
    assert not history.upsert(caption(4, text="stale", revision=1))
    assert not history.upsert(caption(4, text="nonfinal", revision=2, is_final=False))
    after[1]["text"] = "mutated snapshot"
    assert history.snapshot()[1]["text"] == "교정 번역"


def test_remove_retracts_record_and_late_update_cannot_revive_it(tmp_path):
    history = CaptionHistory(tmp_path)
    history.upsert(caption(1))
    history.upsert(caption(2))
    assert history.remove("session-one", 1)
    assert not history.remove("session-one", 99)
    assert not history.upsert(caption(1, revision=3))
    assert not history.upsert(caption(99))
    assert [row["segment_id"] for row in history.snapshot()] == [2]
    assert json.loads(history.path.read_text(encoding="utf-8")) == history.snapshot()


def test_utf8_text_and_only_explicit_fields_are_written(tmp_path):
    history = CaptionHistory(tmp_path)
    history.upsert(caption(1, source_text="你好。こんにちは。", text="안녕하세요.",
                           api_key="must-not-be-written", audio="must-not-be-written",
                           translation_model="ignored", timestamp="caller-controlled"))
    raw = history.path.read_text(encoding="utf-8")
    assert "你好。こんにちは。" in raw and "안녕하세요." in raw
    assert "must-not-be-written" not in raw and "caller-controlled" not in raw
    row = history.snapshot()[0]
    assert set(row) == {"timestamp", "source_text", "text", "language", "target_language", "status",
                        "is_final", "revision", "session_id", "segment_id"}
    assert row["timestamp"].endswith("+00:00")


@pytest.mark.parametrize("bad", [b"{broken json", b'{"unexpected":"object"}', b"x" * (4 * 1024 * 1024 + 1)],
                         ids=["invalid-json", "invalid-root", "oversize"])
def test_corrupt_file_is_preserved_until_next_good_write_with_one_backup(tmp_path, bad):
    path = tmp_path / "logs/caption-history.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(bad)
    history = CaptionHistory(tmp_path)
    assert history.snapshot() == [] and history.load_warning
    assert path.read_bytes() == bad
    history.upsert(caption(1))
    backup = path.with_suffix(".json.corrupt")
    assert backup.read_bytes() == bad
    path.write_bytes(b"second corruption")
    restarted = CaptionHistory(tmp_path)
    restarted.upsert(caption(2))
    assert backup.read_bytes() == b"second corruption"
    assert list(path.parent.glob("*.corrupt")) == [backup]


def test_invalid_rows_are_skipped_and_only_latest_valid_limit_is_loaded(tmp_path):
    history = CaptionHistory(tmp_path)
    for segment in range(1, 5):
        history.upsert(caption(segment))
    rows = history.snapshot()
    history.path.write_text(json.dumps([{"bad": "row"}, *rows]), encoding="utf-8")
    loaded = CaptionHistory(tmp_path, limit=2)
    assert loaded.load_warning
    assert [row["segment_id"] for row in loaded.snapshot()] == [4, 3]


def test_write_failure_propagates_without_losing_previous_file_or_memory(tmp_path, monkeypatch):
    history = CaptionHistory(tmp_path)
    history.upsert(caption(1))
    before = history.path.read_bytes()
    import engine.history as module
    replace = module.os.replace

    def fail_replace(*args):
        raise PermissionError("simulated replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        history.upsert(caption(2))
    assert history.path.read_bytes() == before
    assert [row["segment_id"] for row in history.snapshot()] == [1]
    assert list(history.path.parent.glob(".caption-history-*.tmp")) == []
    monkeypatch.setattr(module.os, "replace", replace)
    assert history.upsert(caption(2))


def test_seen_id_memory_is_bounded_and_oversized_rows_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.history.MAX_SEEN_IDS", 12)
    history = CaptionHistory(tmp_path, limit=3)
    for segment in range(20):
        history.upsert(caption(segment))
    assert len(history._seen) == 12
    before = history.snapshot()
    with pytest.raises(ValueError):
        history.upsert(caption(30, source_text="x" * 6001))
    assert history.snapshot() == before


def windows_permission_error(code):
    error = PermissionError(errno.EACCES, "simulated Windows replacement conflict")
    error.winerror = code
    return error


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_windows_replace_conflict_retries_and_keeps_corrected_row(tmp_path, monkeypatch, winerror):
    import engine.history as module
    history = CaptionHistory(tmp_path)
    history.upsert(caption(1))
    timestamp = history.snapshot()[0]["timestamp"]
    original_replace = module.os.replace
    calls, delays = [], []

    def conflict_twice(source, destination):
        calls.append((source, destination))
        if len(calls) <= 2:
            raise windows_permission_error(winerror)
        return original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", conflict_twice)
    monkeypatch.setattr(module.time, "sleep", delays.append)
    assert history.upsert(caption(1, text="최종 교정", is_final=True, revision=2))
    assert len(calls) == 3 and delays == [.01, .03]
    assert all(source == calls[0][0] and destination == history.path for source, destination in calls)
    rows = history.snapshot()
    assert len(rows) == 1 and rows[0]["is_final"] and rows[0]["revision"] == 2
    assert rows[0]["text"] == "최종 교정" and rows[0]["timestamp"] == timestamp
    assert json.loads(history.path.read_text(encoding="utf-8")) == rows
    assert list(history.path.parent.glob(".caption-history-*.tmp")) == []


def test_persistent_windows_replace_conflict_has_bounded_retries_and_preserves_previous_log(tmp_path, monkeypatch):
    import engine.history as module
    history = CaptionHistory(tmp_path)
    history.upsert(caption(1))
    before_bytes, before_rows = history.path.read_bytes(), history.snapshot()
    calls, delays = [], []

    def always_conflict(source, destination):
        calls.append((source, destination))
        raise windows_permission_error(32)

    monkeypatch.setattr(module.os, "replace", always_conflict)
    monkeypatch.setattr(module.time, "sleep", delays.append)
    with pytest.raises(PermissionError) as caught:
        history.upsert(caption(1, is_final=True, revision=2))
    assert caught.value.winerror == 32
    assert len(calls) == 4 and delays == [.01, .03, .06]
    assert history.path.read_bytes() == before_bytes and history.snapshot() == before_rows
    assert list(history.path.parent.glob(".caption-history-*.tmp")) == []


@pytest.mark.parametrize("error", [OSError(errno.ENOSPC, "simulated full disk"),
                                   OSError(errno.EIO, "simulated I/O failure")])
def test_non_permission_replace_failure_is_not_retried(tmp_path, monkeypatch, error):
    import engine.history as module
    history = CaptionHistory(tmp_path)
    history.upsert(caption(1))
    before = history.path.read_bytes()
    calls, delays = [], []

    def fail_replace(source, destination):
        calls.append((source, destination))
        raise error

    monkeypatch.setattr(module.os, "replace", fail_replace)
    monkeypatch.setattr(module.time, "sleep", delays.append)
    with pytest.raises(OSError) as caught:
        history.upsert(caption(2))
    assert caught.value is error
    assert len(calls) == 1 and delays == []
    assert history.path.read_bytes() == before
    assert [row["segment_id"] for row in history.snapshot()] == [1]
