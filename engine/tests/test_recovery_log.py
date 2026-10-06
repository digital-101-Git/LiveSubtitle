"""CPU-only numeric diagnostic privacy, bounded rotation and shared deletion."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading

import pytest

from engine.log_files import delete_log_files
from engine.recovery_log import RecoveryLog, LOG_NAME, BACKUP_NAME, MAX_FILE_BYTES
import engine.recovery_log as module


def event(**updates):
    return {"session_id": "a"*32, "stage": "input", "code": "input_backlog_dropped",
            "dropped_audio_seconds": 1.2, "dropped_items": 2, "buffer_seconds": 10.1,
            "queue_items": 4, "asr_seconds": .3, "translation_seconds": .5, **updates}


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_only_fixed_identifiers_numbers_and_automatic_utc_are_written(tmp_path):
    record = event()
    RecoveryLog(tmp_path).write(record)
    saved = rows(tmp_path/"logs"/LOG_NAME)
    assert len(saved) == 1 and {k:v for k,v in saved[0].items() if k != "timestamp"} == record
    assert "timestamp" not in record
    timestamp = saved[0]["timestamp"]
    assert timestamp.endswith("Z")
    assert datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    assert abs((datetime.now(timezone.utc)-datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()) < 5
    assert (tmp_path/"logs"/LOG_NAME).read_bytes().endswith(b"\n")


@pytest.mark.parametrize("field", ["text", "source_text", "translation", "prompt", "api_key",
    "exception", "error", "error_code", "audio", "timestamp", "private-secret-key"])
def test_unknown_private_fields_rejected_without_disk_or_exception_echo(tmp_path, field):
    with pytest.raises(ValueError) as error:
        RecoveryLog(tmp_path).write(event(**{field: "private-secret-value"}))
    assert "private-secret" not in str(error.value)
    assert not (tmp_path/"logs").exists()


@pytest.mark.parametrize("updates", [
    {"session_id":"token-or-caption"}, {"session_id":"A"*32}, {"session_id":"a"*31},
    {"session_id":"a"*32+"\n"}, {"session_id":123}, {"stage":"caption body"},
    {"stage":[]}, {"code":"private-secret-exception"}, {"code":None},
    {"queue_items":True}, {"queue_items":1.0}, {"queue_items":-1}, {"queue_items":2**31},
    {"dropped_items":"2"}, {"buffer_seconds":float("nan")}, {"asr_seconds":float("inf")},
    {"translation_seconds":-0.1}, {"dropped_audio_seconds":True},
    {"asr_seconds":"0.2"}, {"buffer_seconds":10**1000},
])
def test_invalid_identifiers_and_non_numeric_unbounded_values_are_rejected(tmp_path, updates):
    with pytest.raises(ValueError) as error:
        RecoveryLog(tmp_path).write(event(**updates))
    assert "private-secret" not in str(error.value)
    assert not (tmp_path/"logs").exists()


@pytest.mark.parametrize("value", [None, [], {}, {"session_id":"a"*32,"stage":"asr"}])
def test_required_event_schema(tmp_path, value):
    with pytest.raises(ValueError):
        RecoveryLog(tmp_path).write(value)
    assert not (tmp_path/"logs").exists()


def test_all_integration_codes_and_optional_numeric_fields(tmp_path):
    writer = RecoveryLog(tmp_path)
    for stage,code in [("input","input_backlog_dropped"),("asr","asr_backlog_dropped"),
            ("translation","translation_backlog_dropped"),("session","buffer_recovered"),
            ("session","session_error")]:
        writer.write({"session_id":"0123456789abcdef"*2,"stage":stage,"code":code})
    assert len(rows(tmp_path/"logs"/LOG_NAME)) == 5


def test_rotation_keeps_only_one_bounded_backup_and_complete_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(module,"MAX_FILE_BYTES",750)
    writer=RecoveryLog(tmp_path)
    for index in range(20):
        writer.write(event(queue_items=index))
        files=list((tmp_path/"logs").iterdir())
        assert {p.name for p in files} <= {LOG_NAME,BACKUP_NAME}
        assert len(files) <= 2 and sum(p.stat().st_size for p in files) <= 1500
        assert all(p.stat().st_size <= 750 for p in files)
        assert all(rows(p) for p in files)
    before=rows(tmp_path/"logs"/BACKUP_NAME)
    after=rows(tmp_path/"logs"/LOG_NAME)
    indices=[r["queue_items"] for r in before+after]
    assert indices == sorted(set(indices)) and indices[-1] == 19 and indices[0] > 0
    assert MAX_FILE_BYTES == 1024*1024


def test_oversized_existing_files_cannot_escape_total_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(module,"MAX_FILE_BYTES",750)
    directory=tmp_path/"logs"; directory.mkdir()
    for name in (LOG_NAME,BACKUP_NAME):
        (directory/name).write_bytes(b"x"*751)
    RecoveryLog(tmp_path).write(event())
    assert len(list(directory.iterdir())) == 1
    assert len(rows(directory/LOG_NAME)) == 1
    assert (directory/LOG_NAME).stat().st_size <= 750


def test_permission_failure_propagates_and_releases_shared_lock(tmp_path, monkeypatch):
    lock=threading.Lock()
    def denied(*args,**kwargs):
        raise PermissionError("synthetic permission failure")
    monkeypatch.setattr(module,"log_directory",denied)
    writer=RecoveryLog(tmp_path,lock)
    with pytest.raises(PermissionError):
        writer.write(event())
    assert lock.acquire(blocking=False)
    lock.release()


def test_failed_rotation_preserves_previous_files(tmp_path, monkeypatch):
    monkeypatch.setattr(module,"MAX_FILE_BYTES",750)
    directory=tmp_path/"logs"; directory.mkdir()
    active=directory/LOG_NAME; backup=directory/BACKUP_NAME
    active.write_bytes(b"a"*700); backup.write_bytes(b"b"*400)
    def denied(*args,**kwargs):
        raise PermissionError("synthetic sharing failure")
    monkeypatch.setattr(Path,"replace",denied)
    with pytest.raises(PermissionError):
        RecoveryLog(tmp_path).write(event())
    assert active.read_bytes() == b"a"*700 and backup.read_bytes() == b"b"*400


@pytest.mark.parametrize("name",[LOG_NAME,BACKUP_NAME])
def test_hardlinked_active_or_backup_is_never_modified(tmp_path,name):
    outside=tmp_path/"outside.txt"; outside.write_bytes(b"unchanged")
    directory=tmp_path/"logs"; directory.mkdir()
    os.link(outside,directory/name)
    with pytest.raises(OSError):
        RecoveryLog(tmp_path).write(event())
    assert outside.read_bytes() == b"unchanged"
    assert {p.name for p in directory.iterdir()} == {name}


@pytest.mark.parametrize("kind",["directory","active","backup"])
def test_symlink_paths_are_not_followed(tmp_path,kind):
    outside=tmp_path/"outside"; outside.mkdir()
    outside_file=outside/"keep.txt"; outside_file.write_bytes(b"unchanged")
    logs=tmp_path/"logs"
    try:
        if kind=="directory":
            logs.symlink_to(outside,target_is_directory=True)
        else:
            logs.mkdir()
            (logs/(LOG_NAME if kind=="active" else BACKUP_NAME)).symlink_to(outside_file)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(OSError):
        RecoveryLog(tmp_path).write(event())
    assert outside_file.read_bytes() == b"unchanged"
    assert list(outside.iterdir()) == [outside_file]


def test_invalid_logs_directory_is_rejected(tmp_path):
    path=tmp_path/"logs"; path.write_bytes(b"unchanged")
    with pytest.raises(OSError):
        RecoveryLog(tmp_path).write(event())
    assert path.read_bytes() == b"unchanged"


def test_delete_then_write_recreates_only_new_diagnostics(tmp_path):
    lock=threading.RLock()
    writer=RecoveryLog(tmp_path,lock)
    writer.write(event(queue_items=1))
    with lock:
        assert delete_log_files(tmp_path) == {"ok":True,"deleted_count":1,"failed_files":[]}
    assert not (tmp_path/"logs"/LOG_NAME).exists()
    writer.write(event(queue_items=2))
    assert [r["queue_items"] for r in rows(tmp_path/"logs"/LOG_NAME)] == [2]


def test_shared_lock_serializes_deletion_with_multiple_writers(tmp_path):
    lock=threading.RLock()
    writers=[RecoveryLog(tmp_path,lock),RecoveryLog(tmp_path,lock)]
    writers[0].write(event(queue_items=0))
    started=threading.Event()
    failures=[]
    def write_after_lock():
        started.set()
        try:
            writers[1].write(event(queue_items=1))
        except Exception as exc:
            failures.append(exc)
    with lock:
        thread=threading.Thread(target=write_after_lock)
        thread.start()
        assert started.wait(2)
        assert delete_log_files(tmp_path)["deleted_count"] == 1
        assert not (tmp_path/"logs"/LOG_NAME).exists()
    thread.join(2)
    assert not thread.is_alive() and not failures
    assert [r["queue_items"] for r in rows(tmp_path/"logs"/LOG_NAME)] == [1]
