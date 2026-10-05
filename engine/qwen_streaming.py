"""Timestamp-free, bounded full-utterance re-decoding for local Qwen ASR.

Audio ownership follows real PCM boundaries, never character proportions. Normal
publication requires two expanding observations and a sentence/clause boundary.
The final decode can correct earlier published sentences through replacements.
The controller is pure Python: callers serialize feed/accept on their event loop
and run at most the one issued immutable snapshot in the model worker.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import re
import struct
import unicodedata

from .sentences import completed_sentence_prefix, split_sentences
from .settings import EngineError
from .text_normalization import chinese_comparison_text


@dataclass(frozen=True, slots=True)
class QwenSnapshot:
    window_id: int
    start_sample: int
    end_sample: int
    pcm: bytes
    is_final: bool
    speculative: bool
    reason: str
    speech_revision: int
    last_voiced_end: int
    cached_text: str | None = None
    cached_language: str = "auto"

    @property
    def offset(self) -> float:
        return self.start_sample / 16000


@dataclass(frozen=True, slots=True)
class QwenResult:
    segments: tuple[str, ...] = ()
    pending_text: str = ""
    warnings: tuple[str, ...] = ()
    window_closed: bool = False
    replacement_segments: tuple[str, ...] | None = None
    language: str = "auto"


@dataclass(slots=True)
class _Window:
    identifier: int
    start: int
    pcm: bytearray = field(default_factory=bytearray)
    voiced: int = 0
    silent: int = 0
    rms_silent: int = 0
    neural_speech_seen: bool = False
    speech_revision: int = 0
    last_voiced_end: int = 0
    closed: bool = False
    reason: str = ""
    last_snapshot_end: int = 0
    last_spec_revision: int = -1
    previous: str = ""
    previous_end: int = 0
    latest_nonempty: str = ""
    latest_language: str = "auto"
    published: list[str] = field(default_factory=list)
    revised: bool = False
    warned_revision: bool = False
    cached: tuple[QwenSnapshot, str, str] | None = None

    @property
    def end(self) -> int:
        return self.start + len(self.pcm) // 2


def _cjk(char: str) -> bool:
    return bool(char) and ("\u3400" <= char <= "\u9fff" or
                          "\u3040" <= char <= "\u30ff" or
                          "\uac00" <= char <= "\ud7af")


def _normalized(text: str) -> tuple[str, list[int]]:
    """Comparison key and exact original end indices; original text survives.

    CJK spacing/script and sentence punctuation can change between hypotheses.
    Latin word spaces and decimal points remain significant.
    """
    chars: list[str] = []
    ends: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            before = text[:index].rstrip()[-1:]
            after = text[index + 1:].lstrip()[:1]
            if before and after and not _cjk(before) and not _cjk(after):
                if chars and chars[-1] != " ":
                    chars.append(" ")
                    ends.append(index + 1)
            continue
        if not char.isalnum():
            if char == "." and index and index + 1 < len(text) and text[index-1].isdigit() and text[index+1].isdigit():
                chars.append(char)
                ends.append(index + 1)
            continue
        normalized = chinese_comparison_text(unicodedata.normalize("NFKC", char)).casefold()
        chars.extend(normalized)
        ends.extend([index + 1] * len(normalized))
    while chars and chars[-1] == " ":
        chars.pop()
        ends.pop()
    return "".join(chars), ends


def _key(text: str) -> str:
    return _normalized(text)[0]


def _after_key(text: str, length: int) -> str:
    _, ends = _normalized(text)
    if not length:
        return text.strip()
    if length >= len(ends):
        return ""
    return text[ends[length - 1]:].lstrip(" \t\r\n.,，。!?！？;；:：\"'’”」』")


def _meaningful_clause(text: str) -> bool:
    letters = [char for char in text if char.isalnum()]
    if any(_cjk(char) for char in letters):
        return len(letters) >= 6
    return len(re.findall(r"\b[\w']+\b", text)) >= 4


def _stable_sentences(text: str, stable_length: int) -> tuple[str, ...]:
    """Only explicit sentence or substantial comma/semicolon boundaries."""
    output: list[str] = []
    consumed = 0
    while text:
        sentence = completed_sentence_prefix(text)
        boundary = len(sentence) if sentence else 0
        for match in re.finditer(r"[,，;；]", text):
            end = match.end()
            if (not boundary or end < boundary) and _meaningful_clause(text[:end]):
                # A decimal/thousands grouping does not imply a phrase boundary.
                if end < len(text) and text[end-2:end-1].isdigit() and text[end].isdigit():
                    continue
                boundary = end
                break
        if not boundary:
            break
        piece = text[:boundary]
        width = len(_key(piece))
        if not width or consumed + width > stable_length:
            break
        output.append(piece.strip())
        consumed += width
        text = text[boundary:].lstrip()
        # Account for the inter-word space in normalized Latin text.
        if text and output[-1] and not _cjk(output[-1].rstrip(".,!?;，。！？；")[-1:]) and not _cjk(text[:1]):
            consumed += 1
    return tuple(output)


class QwenStreaming:
    """Feed PCM16/16 kHz; issue and resolve one recognition request at a time.

    ``feed`` may continue while the worker runs. A speculative request becomes
    stale only if its own window receives new voiced frames. At a hard boundary
    all PCM is recognized before the next window starts; no text-based overlap
    deduction can erase a real repeated utterance in another window.
    """
    frame_bytes = 640
    first_observation_seconds = 1.2
    recognition_interval_seconds = 1.0
    speculative_silence_seconds = .2
    final_silence_seconds = .5
    maximum_window_seconds = 12.0
    maximum_buffer_seconds = 30.0
    maximum_windows = 8

    def __init__(self, threshold: float = .002, voiced_detector=None):
        self.threshold = threshold
        self.voiced_detector = voiced_detector
        self._pending = bytearray()
        self._prefix: deque[tuple[int, bytes]] = deque(maxlen=20)
        self._windows: deque[_Window] = deque()
        self._active: _Window | None = None
        self._request: QwenSnapshot | None = None
        self._sample = 0
        self._next_id = 1
        self._stopped = False
        self._finished = False
        self._interval = self.recognition_interval_seconds

    @property
    def buffered_seconds(self) -> float:
        return (len(self._pending) + sum(len(window.pcm) for window in self._windows)
                + sum(len(frame) for _, frame in self._prefix)) / 32000

    @property
    def recognition_pending(self) -> bool:
        return self._request is not None

    def feed(self, pcm: bytes) -> None:
        if self._stopped:
            return
        if self._finished:
            raise RuntimeError("Audio has already ended.")
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) % 2:
            raise ValueError("Expected complete PCM16 samples.")
        if len(pcm) > 32000:
            raise ValueError("Feed at most one second of PCM at a time.")
        if self.buffered_seconds + len(pcm) / 32000 > self.maximum_buffer_seconds:
            raise EngineError("qwen_streaming_overrun", "음성 인식이 입력 속도를 따라가지 못했습니다. 다시 시작해 주세요.")
        self._pending.extend(pcm)
        while len(self._pending) >= self.frame_bytes:
            frame = bytes(self._pending[:self.frame_bytes])
            del self._pending[:self.frame_bytes]
            start = self._sample
            self._sample += 320
            voiced = self._voiced(frame)
            neural_voiced = (bool(self.voiced_detector(frame))
                             if self.voiced_detector is not None else False)
            if self._active is None:
                if not voiced:
                    self._prefix.append((start, frame))
                    continue
                if len(self._windows) >= self.maximum_windows:
                    raise EngineError("qwen_streaming_overrun", "처리 대기 중인 음성 구간이 너무 많습니다. 다시 시작해 주세요.")
                window = _Window(self._next_id, self._prefix[0][0] if self._prefix else start)
                self._next_id += 1
                window.pcm.extend(b"".join(item for _, item in self._prefix))
                self._prefix.clear()
                self._windows.append(window)
                self._active = window
            window = self._active
            window.pcm.extend(frame)
            # RMS retains ownership of audio and minimum utterance length. A
            # neural false negative must never prevent a window from starting.
            window.voiced += int(voiced)
            window.rms_silent = 0 if voiced else window.rms_silent + 1
            window.neural_speech_seen |= neural_voiced
            endpoint_voiced = voiced and (not window.neural_speech_seen or neural_voiced)
            window.silent = 0 if endpoint_voiced else window.silent + 1
            if voiced:
                # Even when the neural hint misses resumed speech, never reuse
                # an early hypothesis that did not include new RMS-positive PCM.
                window.speech_revision += 1
                window.last_voiced_end = self._sample
                window.cached = None
            if window.silent * .02 >= self.final_silence_seconds:
                self._close(window, "silence")
            elif len(window.pcm) / 32000 >= self.maximum_window_seconds:
                self._close(window, "maximum_window")

    def _voiced(self, pcm: bytes) -> bool:
        samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
        return math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768 >= self.threshold

    def _close(self, window: _Window, reason: str) -> None:
        window.closed = True
        window.reason = reason
        self._active = None
        if reason == "silence":
            # A neural endpoint can contain non-silent audio. Do not replay that
            # audio in the next window: only actual low-RMS silence is prefix.
            count = min(20, window.rms_silent)
            if not count:
                return
            tail = bytes(window.pcm[-count * self.frame_bytes:])
            first = window.end - len(tail) // 2
            self._prefix.extend((first + offset // 2, tail[offset:offset+self.frame_bytes])
                                for offset in range(0, len(tail), self.frame_bytes))

    def finish(self) -> None:
        """Explicit end of input, retaining any sub-frame tail for a final decode."""
        if self._stopped or self._finished:
            return
        self._finished = True
        if self._active is not None:
            if self._pending:
                if self.voiced_detector is not None:
                    self.voiced_detector(bytes(self._pending))
                if self._voiced(bytes(self._pending)):
                    self._active.speech_revision += 1
                    self._active.last_voiced_end = self._active.end + len(self._pending) // 2
                    self._active.cached = None
            self._active.pcm.extend(self._pending)
            self._pending.clear()
            self._close(self._active, "end_of_stream")

    def next_snapshot(self) -> QwenSnapshot | None:
        if self._stopped or self._request is not None:
            return None
        while self._windows:
            window = self._windows[0]
            if window.closed and window.voiced < 10:
                self._windows.popleft()
                continue
            speculative = (not window.closed and window.silent * .02 >= self.speculative_silence_seconds
                           and window.last_spec_revision != window.speech_revision)
            due = (window.end - window.start) / 16000 >= self.first_observation_seconds
            due = due and (not window.last_snapshot_end or
                          (window.end - window.last_snapshot_end) / 16000 >= self._interval)
            if not window.closed and not speculative and not due:
                return None
            if window.voiced < 10:
                return None
            cached = window.cached if window.closed else None
            # Empty early recognition is insufficient evidence to skip final ASR.
            if cached and (cached[0].speech_revision != window.speech_revision or not cached[1]):
                cached = None
            snapshot = QwenSnapshot(window.identifier, window.start, window.end,
                                    bytes(window.pcm), window.closed, speculative,
                                    window.reason if window.closed else ("early_silence" if speculative else "observation"),
                                    window.speech_revision, window.last_voiced_end,
                                    cached[1] if cached else None, cached[2] if cached else "auto")
            # A false early pause must not postpone the next regular expanding
            # observation after speech resumes.
            if not speculative:
                window.last_snapshot_end = window.end
            if speculative:
                window.last_spec_revision = window.speech_revision
            self._request = snapshot
            return snapshot
        return None

    @staticmethod
    def _suffix(window: _Window, text: str) -> tuple[str | None, bool]:
        committed = _key(" ".join(window.published))
        current = _key(text)
        if not committed:
            return text.strip(), False
        if current.startswith(committed):
            return _after_key(text, len(committed)), False
        # Never search by a one-character suffix or remove repetitions in a
        # different window. A unique long suffix is only a text alignment hint.
        last = _key(window.published[-1])
        for length in range(min(24, len(last)), 5, -1):
            anchor = last[-length:]
            if len(anchor.replace(" ", "")) < 6:
                continue
            # Latin anchors must begin at a real word boundary.
            if len(last) > length and not _cjk(anchor[:1]) and last[-length-1] != " ":
                continue
            start = current.find(anchor)
            if start >= 0 and current.find(anchor, start + 1) < 0:
                end = start + len(anchor)
                return _after_key(text, end), True
        return None, True

    def accept(self, snapshot: QwenSnapshot, text: str, language: str = "auto",
               *, inference_seconds: float | None = None) -> QwenResult:
        if self._stopped:
            return QwenResult()
        if snapshot is not self._request:
            raise RuntimeError("Recognition does not belong to the outstanding snapshot.")
        self._request = None
        if not isinstance(text, str) or len(text) > 6000:
            raise EngineError("transcript_too_long", "음성 인식 결과의 길이가 올바르지 않습니다.")
        if inference_seconds is not None and math.isfinite(inference_seconds) and inference_seconds >= 0:
            self._interval = min(2.0, max(self.recognition_interval_seconds, inference_seconds * 1.2))
        window = self._windows[0]
        if window.identifier != snapshot.window_id:
            raise RuntimeError("The audio window changed before recognition completed.")
        text = text.strip()
        if snapshot.speculative:
            if snapshot.speech_revision != window.speech_revision:
                return QwenResult(language=language)
            if not window.closed:
                window.cached = (snapshot, text, language)
                return QwenResult(pending_text=text, language=language)
        final = snapshot.is_final or (snapshot.speculative and window.closed and bool(text))
        if snapshot.speculative and window.closed and not text:
            # Force a complete final request after an empty speculative result.
            return QwenResult(language=language)
        warnings: list[str] = []
        if final and window.reason == "maximum_window":
            warnings.append("qwen_streaming_deadline")
        if not text:
            if not final:
                window.previous = ""  # Empty observations never establish stability.
                window.previous_end = snapshot.end_sample
                return QwenResult(language=language)
            if window.latest_nonempty:
                text = window.latest_nonempty
                language = window.latest_language
                warnings.append("qwen_streaming_empty_final")
        else:
            window.latest_nonempty = text
            window.latest_language = language
        suffix, revised = self._suffix(window, text)
        window.revised |= revised
        if revised and not window.warned_revision:
            warnings.append("qwen_streaming_prefix_revised")
            window.warned_revision = True
        if final:
            replacement = tuple(split_sentences(text)) if window.revised else None
            segments = () if replacement is not None else tuple(split_sentences(suffix or ""))
            self._windows.popleft()
            return QwenResult(segments, "", tuple(warnings), True, replacement, language)
        previous_suffix, _ = self._suffix(window, window.previous)
        stable = 0
        if suffix is not None and previous_suffix is not None and snapshot.end_sample > window.previous_end:
            for old, new in zip(_key(previous_suffix), _key(suffix)):
                if old != new:
                    break
                stable += 1
        segments = _stable_sentences(suffix, stable) if suffix is not None else ()
        if segments:
            window.published.extend(segments)
        window.previous = text
        window.previous_end = snapshot.end_sample
        remaining, _ = self._suffix(window, text)
        return QwenResult(segments, remaining if remaining is not None else text,
                          tuple(warnings), language=language)

    def stop(self) -> None:
        """Cancel; a model reply arriving after stop cannot publish or revive PCM."""
        self._stopped = True
        self._request = None
        self._active = None
        self._windows.clear()
        self._pending.clear()
        self._prefix.clear()
