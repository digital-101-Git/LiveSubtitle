"""A local, rotating caption log. No audio, model metadata, keys, or UI restore."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .log_files import delete_log_files


MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SEEN_IDS = 1000


def _replace_file(source: Path, destination: Path) -> None:
    # Windows can briefly deny replacement while another process inspects the
    # file. Retry the same atomic operation, without deleting the old log. This
    # runs on the history worker thread and never stalls subtitle delivery.
    delays = (.01, .03, .06)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def _string(value, name: str, limit: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (nonempty and not value.strip()):
        raise ValueError(f"Invalid caption history {name}")
    value.encode("utf-8")
    return value


def _integer(value, name: str) -> int:
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError(f"Invalid caption history {name}")
    return value


def _key(session_id, segment_id) -> tuple[str, int]:
    return (_string(session_id, "session_id", 128, nonempty=True),
            _integer(segment_id, "segment_id"))


def _row(event: dict, timestamp: str) -> dict:
    if not isinstance(event, dict):
        raise ValueError("Invalid caption history row")
    session_id, segment_id = _key(event.get("session_id"), event.get("segment_id"))
    final = event.get("is_final", True)
    if type(final) is not bool:
        raise ValueError("Invalid caption history is_final")
    # Deliberately enumerate fields: event/API metadata must not leak into logs.
    return {
        "timestamp": timestamp,
        "source_text": _string(event.get("source_text", ""), "source_text", 6000),
        "text": _string(event.get("text", ""), "text", 6000),
        "language": _string(event.get("language", "auto"), "language", 32),
        "target_language": _string(event.get("target_language", "ko"), "target_language", 32),
        "status": _string(event.get("status", event.get("translation_status", "ok")), "status", 64),
        "is_final": final,
        "revision": _integer(event.get("revision", 1), "revision"),
        "session_id": session_id,
        "segment_id": segment_id,
    }


class CaptionHistory:
    def __init__(self, root: Path, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Caption history limit must be between 1 and 100")
        self.path = Path(root) / "logs" / "caption-history.json"
        self.limit = limit
        self.load_warning: str | None = None
        self._rows: list[dict] = []
        self._seen: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._lock = threading.RLock()
        self._needs_backup = False
        self._load()

    def _remember(self, key: tuple[str, int]) -> None:
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > MAX_SEEN_IDS:
            self._seen.popitem(last=False)

    def _load(self) -> None:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("Caption history file exceeds size limit")
            payload = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(payload, list):
                raise ValueError("Caption history must be an array")
            invalid = False
            for event in payload:
                try:
                    timestamp = _string(event.get("timestamp"), "timestamp", 48, nonempty=True)
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError("Caption history timestamp must include timezone")
                    row = _row(event, parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds"))
                except (AttributeError, TypeError, ValueError, OverflowError):
                    invalid = True
                    continue
                key = _key(row["session_id"], row["segment_id"])
                if key in self._seen:
                    continue
                self._rows.append(row)
                self._remember(key)
                if len(self._rows) == self.limit:
                    break
            if invalid:
                self._needs_backup = True
                self.load_warning = "번역 로그의 잘못된 항목을 제외했습니다. 원본은 다음 정상 기록 때 백업합니다."
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            self._rows = []
            self._seen.clear()
            self._needs_backup = True
            self.load_warning = "기존 번역 로그를 읽지 못했습니다. 원본은 다음 정상 기록 때 백업하고 새 로그를 기록합니다."

    def snapshot(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._rows)

    def clear_logs(self, log_lock) -> dict:
        """Clear disk and memory together after queued pre-clear captions drain."""
        with self._lock, log_lock:
            result = delete_log_files(self.path.parent.parent)
            self._rows.clear()
            # Keep retired IDs: a revision of a deleted caption must not restore
            # it. New segment IDs continue to be recorded normally.
            self._needs_backup = False
            self.load_warning = None
            return result

    def upsert(self, event: dict) -> bool:
        """Write a new/corrected caption; False means an unchanged or retired ID.

        A bounded 1,000-ID memory suppresses late revisions to recently rotated
        records without assuming that segment IDs arrive in numeric order.
        """
        with self._lock:
            if not isinstance(event, dict):
                raise ValueError("Invalid caption history row")
            key = _key(event.get("session_id"), event.get("segment_id"))
            index = next((i for i, row in enumerate(self._rows)
                          if (row["session_id"], row["segment_id"]) == key), None)
            if index is None and key in self._seen:
                return False
            timestamp = (self._rows[index]["timestamp"] if index is not None
                         else datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
            row = _row(event, timestamp)
            rows = self._rows.copy()
            if index is not None:
                previous = rows[index]
                if (row["revision"] < previous["revision"]
                        or (row["revision"] == previous["revision"]
                            and previous["is_final"] and not row["is_final"])
                        or row == previous):
                    return False
                rows[index] = row
            else:
                rows.insert(0, row)
                del rows[self.limit:]
            self._save(rows)
            self._rows = rows
            self._remember(key)
            return True

    def remove(self, session_id: str, segment_id: int | str) -> bool:
        with self._lock:
            key = _key(session_id, segment_id)
            rows = [row for row in self._rows if (row["session_id"], row["segment_id"]) != key]
            if len(rows) == len(self._rows):
                self._remember(key)
                return False
            self._save(rows)
            self._rows = rows
            self._remember(key)
            return True

    def _save(self, rows: list[dict]) -> None:
        payload = (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError("Caption history file exceeds 4 MiB")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent,
                                             prefix=".caption-history-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if self._needs_backup:
                if self.path.exists():
                    _replace_file(self.path, self.path.with_suffix(".json.corrupt"))
                self._needs_backup = False
            _replace_file(temporary, self.path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
