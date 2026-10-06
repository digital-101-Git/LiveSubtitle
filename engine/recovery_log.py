"""Bounded local recovery diagnostics: fixed identifiers and numbers only.

The caller queues work outside the event loop and handles write failures. Pass
runtime.log_lock so log deletion and this short-lived writer share one lock.
No audio, caption text, credentials, arbitrary error codes, or exceptions are
accepted. Invalid events fail before any directory or file is created.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading

from .log_files import log_directory, regular_log_path


MAX_FILE_BYTES = 1024 * 1024
LOG_NAME = "stream-recovery.jsonl"
BACKUP_NAME = "stream-recovery.jsonl.1"
STAGES = frozenset({"input", "asr", "translation", "session"})
CODES = frozenset({"input_backlog_dropped", "asr_backlog_dropped",
                   "translation_backlog_dropped", "buffer_recovered", "session_error"})
SECONDS_FIELDS = frozenset({"dropped_audio_seconds", "buffer_seconds",
                            "asr_seconds", "translation_seconds"})
COUNT_FIELDS = frozenset({"dropped_items", "queue_items"})
REQUIRED_FIELDS = frozenset({"session_id", "stage", "code"})
ALLOWED_FIELDS = REQUIRED_FIELDS | SECONDS_FIELDS | COUNT_FIELDS
_SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")


def _row(event: dict) -> dict:
    # Do not interpolate rejected keys/values into an error message: callers
    # may forward the exception through other diagnostics.
    if type(event) is not dict or not REQUIRED_FIELDS <= event.keys() or not event.keys() <= ALLOWED_FIELDS:
        raise ValueError("Invalid recovery log fields")
    session_id = event["session_id"]
    if type(session_id) is not str or _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("Invalid recovery log session identifier")
    stage, code = event["stage"], event["code"]
    if type(stage) is not str or stage not in STAGES:
        raise ValueError("Invalid recovery log stage")
    if type(code) is not str or code not in CODES:
        raise ValueError("Invalid recovery log code")
    row = {"session_id": session_id, "stage": stage, "code": code}
    for name in sorted(SECONDS_FIELDS):
        if name in event:
            value = event[name]
            if type(value) not in (int, float) or not 0 <= value <= 1_000_000_000 or not math.isfinite(value):
                raise ValueError("Invalid recovery log duration")
            row[name] = value
    for name in sorted(COUNT_FIELDS):
        if name in event:
            value = event[name]
            if type(value) is not int or not 0 <= value <= 2**31 - 1:
                raise ValueError("Invalid recovery log count")
            row[name] = value
    return row


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


class RecoveryLog:
    """Synchronous writer, at most 1 MiB active plus one 1 MiB backup.

    Files are opened only during write(). Permission/path failures propagate.
    The optional lock must be the same lock held by the log-deletion caller;
    the default protects one standalone writer, not separate instances.
    """
    def __init__(self, root: Path, lock=None):
        self.root = Path(root)
        self.lock = threading.RLock() if lock is None else lock

    def write(self, event: dict) -> None:
        row = _row(event)
        with self.lock:
            row["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            data = (json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("Recovery log row exceeds size limit")
            directory = log_directory(self.root, create=True)
            # Validate both names, including the rotation destination, before
            # mutating either path. Existing link/reparse/hardlink protection
            # is shared with the application's other log writers/deletion.
            path = regular_log_path(directory, LOG_NAME)
            backup = regular_log_path(directory, BACKUP_NAME)
            size, backup_size = _size(path), _size(backup)
            if backup_size > MAX_FILE_BYTES:
                backup.unlink()
            if size > MAX_FILE_BYTES:
                # Do not rotate an externally enlarged/pre-existing file into
                # a backup that would violate the total two-file size bound.
                path.unlink()
            elif size and size + len(data) > MAX_FILE_BYTES:
                path.replace(backup)
            with path.open("ab") as stream:
                written = stream.write(data)
                if written != len(data):
                    raise OSError("Incomplete recovery log write")
