"""Bounded stdout draining with short-lived, deletion-compatible log writes."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import BinaryIO

from .log_files import log_directory, regular_log_path


class PipeLogWriter:
    """Own one child's stdout pipe; never retain an open log file.

    Disk/permission failures or a busy deletion lock discard a bounded chunk,
    but never prevent the next pipe read. No queue or whole-output buffer is
    retained. finish() waits briefly for normal EOF, then requests cancellation.
    """
    CHUNK_BYTES = 8192
    POLL_SECONDS = 0.025

    def __init__(self, pipe: BinaryIO, root: Path, lock, *, secrets: tuple[str, ...] = ()):
        self.pipe = pipe
        self.root = root
        self.lock = lock
        self._stop = threading.Event()
        self._secrets = tuple(value.encode('utf-8') for value in secrets if value)
        self.thread = threading.Thread(target=self._run, name='llama-log-pump', daemon=True)

    def start(self) -> None:
        self.thread.start()

    def finish(self, timeout: float = 1.0) -> bool:
        """Called after process shutdown; at most two bounded join intervals."""
        if self.thread.ident is None:
            self.pipe.close()
            return True
        self.thread.join(timeout)
        if self.thread.is_alive():
            self._stop.set()
            self.thread.join(timeout)
        return not self.thread.is_alive()

    def _read_chunks(self):
        try:
            descriptor = self.pipe.fileno()
        except (AttributeError, OSError, ValueError):
            # In-memory test streams have no OS pipe and always bounded reads.
            descriptor = None
        if os.name == 'nt' and descriptor is not None:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            peek = kernel.PeekNamedPipe
            peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                             ctypes.POINTER(wintypes.DWORD)]
            peek.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(descriptor)
            while not self._stop.is_set():
                available = wintypes.DWORD()
                if not peek(handle, None, 0, None, ctypes.byref(available), None):
                    # A closed writer reports BROKEN_PIPE; all other pipe
                    # failures also mean it can no longer be safely read.
                    return
                if not available.value:
                    self._stop.wait(self.POLL_SECONDS)
                    continue
                chunk = self.pipe.read(min(self.CHUNK_BYTES, available.value))
                if not chunk:
                    return
                yield chunk
            return
        while not self._stop.is_set():
            if descriptor is not None:
                import select
                if not select.select([descriptor], [], [], self.POLL_SECONDS)[0]:
                    continue
            chunk = self.pipe.read(self.CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    def _append(self, chunk: bytes) -> None:
        if not chunk or not self.lock.acquire(blocking=False):
            return
        try:
            directory = log_directory(self.root, create=True)
            path = regular_log_path(directory, 'llama.log')
            with path.open('ab') as stream:
                stream.write(chunk)
        except Exception:
            # Logging must not terminate or stall the inference process. In
            # particular, do not report this failure into another log handler.
            pass
        finally:
            self.lock.release()

    def _run(self) -> None:
        pending = b''
        try:
            for chunk in self._read_chunks():
                pending += chunk
                # The local llama authentication key is never logged, including
                # when its spelling straddles OS pipe-read boundaries.
                for value in self._secrets:
                    pending = pending.replace(value, b'[REDACTED]')
                keep = 0
                for value in self._secrets:
                    for size in range(min(len(value)-1, len(pending)), keep, -1):
                        if pending.endswith(value[:size]):
                            keep = size
                            break
                if len(pending) > keep:
                    count = len(pending) - keep
                    self._append(pending[:count])
                    pending = pending[count:]
        except (OSError, ValueError):
            pass
        finally:
            # Small bounded redaction suffix only, not accumulated stdout.
            self._append(pending)
            try:
                self.pipe.close()
            except OSError:
                pass
