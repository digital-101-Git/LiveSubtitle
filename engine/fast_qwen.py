"""Final-only Qwen chunks with conservative early recognition and VAD hints.

PCM ownership follows the legacy four-second/200-ms-overlap policy. RMS opens
windows; the optional neural detector can only help end an already active one.
It cannot discard audio. All mutable methods run on the caller's event loop.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import struct

from .settings import EngineError


@dataclass(frozen=True, slots=True)
class FastSnapshot:
    window_id: int
    start_sample: int
    end_sample: int
    pcm: bytes
    is_final: bool
    speculative: bool
    reason: str
    speech_revision: int
    overlaps_previous: bool
    cached_text: str | None = None
    cached_language: str = "auto"


@dataclass(frozen=True, slots=True)
class FastResult:
    text: str = ""
    language: str = "auto"
    is_final: bool = False
    overlaps_previous: bool = False
    reason: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class _Window:
    identifier: int
    start: int
    pcm: bytearray = field(default_factory=bytearray)
    overlaps_previous: bool = False
    continuation: bool = False
    voiced: int = 0
    silent: int = 0
    rms_silent: int = 0
    neural_seen: bool = False
    speech_revision: int = 0
    pause_id: int = 0
    last_spec_pause: int = -1
    closed: bool = False
    reason: str = ""
    cached: tuple[FastSnapshot, str, str] | None = None
    had_early_text: bool = False

    @property
    def end(self):
        return self.start + len(self.pcm) // 2


class FastQwenController:
    frame_bytes = 640
    maximum_window_seconds = 4.0
    speculative_silence_seconds = .2
    final_silence_seconds = .5
    # A four-second in-flight window plus at most about six seconds queued.
    maximum_buffer_seconds = 10.0
    maximum_windows = 8

    def __init__(self, *, threshold=.002, voiced_detector=None):
        self.threshold = threshold
        self.voiced_detector = voiced_detector
        self._pending = bytearray()
        self._prefix = deque(maxlen=20)
        self._windows = deque()
        self._active = None
        self._request = None
        self._sample = 0
        self._next_id = 1
        self._stopped = False
        self._finished = False
        self._pending_continuation = False
        self.statistics = {"speculative_requests": 0, "cache_reuses": 0,
                           "stale_speculation": 0, "rms_endpoints": 0,
                           "neural_endpoints": 0, "maximum_endpoints": 0}

    @property
    def buffered_seconds(self):
        return (len(self._pending) + sum(len(w.pcm) for w in self._windows)
                + sum(len(frame) for _, frame in self._prefix)) / 32000

    @property
    def recognition_pending(self):
        return self._request is not None

    def _rms_voice(self, pcm):
        samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
        return math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768 >= self.threshold

    def _new_window(self, start, pcm=b"", overlaps_previous=False, *, continuation=False):
        if len(self._windows) >= self.maximum_windows:
            raise EngineError("fast_qwen_overrun", "처리 대기 중인 음성 구간이 너무 많습니다. 다시 시작해 주세요.")
        window = _Window(self._next_id, start, bytearray(pcm), overlaps_previous,
                         continuation=continuation)
        self._next_id += 1
        self._windows.append(window)
        self._active = window
        return window

    def feed(self, pcm):
        if self._stopped:
            return
        if self._finished:
            raise RuntimeError("Audio has already ended.")
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) % 2 or len(pcm) > 32000:
            raise ValueError("Expected at most one second of complete PCM16 samples.")
        if self.buffered_seconds + len(pcm) / 32000 > self.maximum_buffer_seconds:
            raise EngineError("fast_qwen_overrun", "음성 인식이 입력 속도를 따라가지 못했습니다. 다시 시작해 주세요.")
        self._pending.extend(pcm)
        while len(self._pending) >= self.frame_bytes:
            frame = bytes(self._pending[:self.frame_bytes])
            del self._pending[:self.frame_bytes]
            start = self._sample
            self._sample += 320
            rms_voice = self._rms_voice(frame)
            neural_voice = bool(self.voiced_detector(frame)) if self.voiced_detector else False
            if self._active is None:
                if not rms_voice:
                    self._pending_continuation = False
                    self._prefix.append((start, frame))
                    continue
                self._new_window(self._prefix[0][0] if self._prefix else start,
                                 b"".join(item for _, item in self._prefix),
                                 continuation=self._pending_continuation)
                self._pending_continuation = False
                self._prefix.clear()
            window = self._active
            # A forced carry is a continuation only if new speech directly
            # follows the cut. Preserve a captured short tail through its
            # ending silence, but do not relax the minimum for a new burst.
            if not rms_voice and window.voiced == 0:
                window.continuation = False
            window.pcm.extend(frame)
            window.voiced += int(rms_voice)
            window.rms_silent = 0 if rms_voice else window.rms_silent + 1
            window.neural_seen |= neural_voice
            endpoint_voice = rms_voice and (not window.neural_seen or neural_voice)
            if endpoint_voice:
                window.silent = 0
            else:
                if not window.silent:
                    window.pause_id += 1
                window.silent += 1
            if rms_voice:
                # A negative neural hint cannot prove new positive-RMS audio is
                # harmless music. Re-decode it at final rather than hide speech
                # missed by VAD behind a cached early transcript.
                window.speech_revision += 1
                window.cached = None
            if window.silent * .02 >= self.final_silence_seconds:
                reason = "rms_silence" if window.rms_silent >= 25 else "neural_silence"
                self._close(window, reason)
            elif len(window.pcm) / 32000 >= self.maximum_window_seconds:
                self._close(window, "maximum_window")

    def _close(self, window, reason):
        window.closed, window.reason = True, reason
        self._active = None
        # A neural hint can cut continuous speech. The immediately following
        # positive-RMS tail belongs to that utterance even when shorter than
        # the usual 200-ms minimum. Actual RMS silence breaks this continuity.
        self._pending_continuation = reason == "neural_silence" and window.rms_silent == 0
        if reason == "maximum_window":
            self.statistics["maximum_endpoints"] += 1
            # Exactly the legacy 200-ms forced-cut overlap; no text timing.
            tail = bytes(window.pcm[-6400:])
            self._new_window(window.end - len(tail)//2, tail, True,
                             continuation=(window.rms_silent == 0 and
                                           window.voiced >= (1 if window.continuation else 10)))
        elif reason in {"rms_silence", "neural_silence"}:
            self.statistics["rms_endpoints" if reason == "rms_silence" else "neural_endpoints"] += 1
            # Copy only actual low-RMS silence. A neural endpoint may contain
            # real speech in its tail, which must not be replayed as a new line.
            count = min(20, window.rms_silent)
            if count:
                tail = bytes(window.pcm[-count*self.frame_bytes:])
                first = window.end - len(tail)//2
                self._prefix.extend((first+i//2, tail[i:i+self.frame_bytes])
                                    for i in range(0, len(tail), self.frame_bytes))

    def next_snapshot(self):
        if self._stopped or self._request is not None:
            return None
        while self._windows:
            window = self._windows[0]
            has_speech = window.voiced >= (1 if window.continuation else 10)
            if window.closed and not has_speech:
                self._windows.popleft()
                continue
            # Neural-only quiet can still contain music or missed speech. Any
            # positive-RMS continuation invalidates early text, so decoding it
            # early would usually just add a second full model call. Precompute
            # only during actual RMS quiet; neural final endpoints stay active.
            speculative = (not window.closed and window.silent*.02 >= self.speculative_silence_seconds
                           and window.rms_silent*.02 >= self.speculative_silence_seconds
                           and window.last_spec_pause != window.pause_id)
            if not has_speech or not (window.closed or speculative):
                return None
            cached = window.cached if window.closed else None
            if cached and (cached[0].speech_revision != window.speech_revision or not cached[1]):
                cached = None
            snapshot = FastSnapshot(window.identifier, window.start, window.end, bytes(window.pcm),
                                    window.closed, speculative, window.reason if window.closed else "early_silence",
                                    window.speech_revision, window.overlaps_previous,
                                    cached[1] if cached else None, cached[2] if cached else "auto")
            if speculative:
                window.last_spec_pause = window.pause_id
                self.statistics["speculative_requests"] += 1
            if cached:
                self.statistics["cache_reuses"] += 1
            self._request = snapshot
            return snapshot
        return None

    def accept(self, snapshot, text, language="auto"):
        if self._stopped:
            return FastResult()
        if snapshot is not self._request:
            raise RuntimeError("Recognition does not belong to the outstanding snapshot.")
        self._request = None
        if not isinstance(text, str) or len(text) > 6000:
            raise EngineError("transcript_too_long", "음성 인식 결과의 길이가 올바르지 않습니다.")
        window = self._windows[0]
        if window.identifier != snapshot.window_id:
            raise RuntimeError("The audio window changed before recognition completed.")
        text = text.strip()
        if snapshot.speculative:
            window.had_early_text |= bool(text)
            if snapshot.speech_revision != window.speech_revision:
                self.statistics["stale_speculation"] += 1
                return FastResult()
            if not window.closed:
                window.cached = (snapshot, text, language)
                return FastResult()
            if not text:
                return FastResult()  # Empty early ASR cannot replace final ASR.
            self.statistics["cache_reuses"] += 1
        self._windows.popleft()
        warnings = ("fast_qwen_empty_final",) if not text and window.had_early_text else ()
        return FastResult(text, language, True, window.overlaps_previous, window.reason, warnings)

    def finish(self):
        if self._stopped or self._finished:
            return
        self._finished = True
        if self._active is not None:
            if self._pending:
                if self.voiced_detector:
                    self.voiced_detector(bytes(self._pending))
                if self._rms_voice(self._pending):
                    self._active.speech_revision += 1
                    self._active.cached = None
                self._active.pcm.extend(self._pending)
                self._pending.clear()
            self._close(self._active, "end_of_stream")

    def stop(self):
        self._stopped = True
        self._request = self._active = None
        self._pending_continuation = False
        self._windows.clear()
        self._prefix.clear()
        self._pending.clear()
