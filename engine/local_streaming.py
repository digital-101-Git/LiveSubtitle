"""Bounded local Whisper streaming, independent of models and async transports.

Call feed(), then snapshot(), recognize that immutable PCM, and accept() its
word timings. Only new audio can trigger a new observation. Normally two
observations must agree before a sentence or clause is committed. A bounded
deadline can publish the latest recognition with an explicit warning; silence
finishes the remaining utterance. Pending words retain their audio for revision.
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
class ASRWord:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class ASRSnapshot:
    window_id: int
    sequence: int
    pcm: bytes
    offset_seconds: float
    is_final: bool
    voiced_frames: int
    context_in_audio: bool = False
    speculative: bool = False
    cached_words: tuple[ASRWord, ...] | None = None
    speech_revision: int = 0
    last_voiced_end: int = 0


@dataclass(frozen=True, slots=True)
class StreamingResult:
    segments: tuple[str, ...]
    committed_text: str
    pending_text: str
    warnings: tuple[str, ...] = ()
    reset_prompt: bool = False


@dataclass(slots=True)
class _Window:
    identifier: int
    offset_samples: int
    pcm: bytearray = field(default_factory=bytearray)
    voiced_frames: int = 0
    silent_frames: int = 0
    ended: bool = False
    last_snapshot_end: int | None = None
    previous_voiced_frames: int = 0
    previous_words: tuple[ASRWord, ...] = ()
    had_recognized_words: bool = False
    context_words: tuple[ASRWord, ...] = ()
    context_frontier: float | None = None
    pending_words: tuple[ASRWord, ...] = ()
    empty_observations: int = 0
    pending_recovered: bool = False
    previous_audio_end: float | None = None
    last_publication_audio_end: float | None = None
    pending_context_misses: int = 0
    speech_revision: int = 0
    last_voiced_end: int = 0
    early_revision: int = -1
    early_words: tuple[ASRWord, ...] | None = None
    early_words_offset: int | None = None
    early_words_revision: int = -1


def _agreement_key(text: str) -> str:
    # CJK tokenization can change between updates (师娘|们 vs 师|娘们).
    # Script switches are formatting changes, not new spoken Chinese words.
    # Keep punctuation and numbers meaningful, and never alter displayed text.
    text = chinese_comparison_text(unicodedata.normalize("NFC", text))
    return "".join(character for character in text.casefold()
                   if not character.isspace())


def _append_text(left: str, right: str) -> str:
    if not left:
        return right.lstrip()
    # The first word after a trimmed PCM boundary may lose its leading space.
    if left[-1:].isascii() and left[-1:].isalnum() and right[:1].isascii() and right[:1].isalnum():
        return left + " " + right
    return left + right


def _is_cjk_character(character: str) -> bool:
    if not character:
        return False
    value = ord(character)
    return any(start <= value <= end for start, end in (
        (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
        (0x20000, 0x2FA1F), (0x30000, 0x323AF),  # Han ideographs
        (0x3040, 0x30FF), (0x31F0, 0x31FF),      # Hiragana / Katakana
        (0x1100, 0x11FF), (0x3130, 0x318F),      # Hangul letters / syllables
        (0xA960, 0xA97F), (0xAC00, 0xD7AF), (0xD7B0, 0xD7FF),
    )) and unicodedata.category(character).startswith("L")


def _cjk_space_at(text: str, end: int) -> bool:
    """A recognizer's phrase space, excluding Latin words and numeric tokens."""
    if end <= 0 or end >= len(text) or not text[end].isspace():
        return False
    following = end
    while following < len(text) and text[following].isspace():
        following += 1
    return (_is_cjk_character(text[end - 1:end])
            and _is_cjk_character(text[following:following + 1]))


class LocalWhisperStreaming:
    sample_rate = 16000
    frame_samples = 320
    frame_bytes = 640
    prefix_frames = 20
    silence_frames = 25
    early_silence_frames = 10
    phrase_min_characters = 12
    cjk_space_min_characters = 6
    phrase_pause_seconds = .3
    short_phrase_pause_seconds = .6
    short_phrase_end_seconds = .8
    fallback_wait_seconds = 3.5
    fallback_target_seconds = 3.0
    fallback_minimum_seconds = 2.5
    fallback_maximum_seconds = 3.5
    context_trim_seconds = 8.0
    retained_context_seconds = 3.0
    context_timing_tolerance = .12
    deadline_seconds = 8.0
    deadline_tail_seconds = .5
    segment_deadline_seconds = 12.0

    def __init__(self, *, threshold: float = .002, update_seconds: float = 1.0,
                 max_buffer_seconds: float = 24.0, end_guard_seconds: float = .25,
                 early_transcription: bool = False, voiced_detector=None):
        if (not math.isfinite(threshold) or threshold < 0
                or not .1 <= update_seconds <= 5
                or not 2 <= max_buffer_seconds <= 30
                or not 0 <= end_guard_seconds <= 1):
            raise ValueError("Invalid local streaming limits")
        self.threshold = threshold
        self.update_samples = round(update_seconds * self.sample_rate)
        self.max_buffer_bytes = round(max_buffer_seconds * self.sample_rate) * 2
        self.end_guard_seconds = end_guard_seconds
        self.early_transcription = early_transcription
        self.voiced_detector = voiced_detector
        self._pending = bytearray()
        self._prefix: deque[bytes] = deque(maxlen=self.prefix_frames)
        self._windows: deque[_Window] = deque()
        self._active: _Window | None = None
        self._clock_samples = 0
        self._next_window = 1
        self._sequence = 0
        self._outstanding: ASRSnapshot | None = None

    @property
    def buffered_seconds(self) -> float:
        return sum(len(window.pcm) for window in self._windows) / (2 * self.sample_rate)

    def feed(self, pcm: bytes) -> None:
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise EngineError("invalid_audio", "오디오는 비어 있지 않은 16kHz mono PCM16LE여야 합니다.")
        # Iterate without copying an arbitrarily large incoming packet. The
        # residual frame is always smaller than 20 ms.
        incoming = memoryview(pcm)
        position = 0
        while position < len(incoming):
            take = min(self.frame_bytes - len(self._pending), len(incoming) - position)
            self._pending.extend(incoming[position:position + take])
            position += take
            if len(self._pending) == self.frame_bytes:
                frame = bytes(self._pending)
                self._pending.clear()
                self._frame(frame)

    def _frame(self, frame: bytes) -> None:
        values = struct.unpack("<320h", frame)
        voiced = (bool(self.voiced_detector(frame)) if self.voiced_detector is not None else
                  math.sqrt(sum(value * value for value in values) / self.frame_samples) / 32768 >= self.threshold)
        if self._active is None:
            if not voiced:
                self._prefix.append(frame)
                self._clock_samples += self.frame_samples
                return
            if sum(len(window.pcm) for window in self._windows) + (len(self._prefix) + 1) * self.frame_bytes > self.max_buffer_bytes:
                raise EngineError("local_streaming_overrun",
                                  "음성 인식 대기 버퍼가 가득 차 누락을 막기 위해 중지했습니다.", 409)
            window = _Window(self._next_window, self._clock_samples - len(self._prefix) * self.frame_samples)
            self._next_window += 1
            window.pcm.extend(b"".join(self._prefix))
            self._prefix.clear()
            self._windows.append(window)
            self._active = window
        if sum(len(window.pcm) for window in self._windows) + len(frame) > self.max_buffer_bytes:
            raise EngineError("local_streaming_overrun",
                              "음성 인식이 확정되지 않아 오디오 버퍼 한도에 도달했습니다. 누락을 막기 위해 중지했습니다.", 409)
        window = self._active
        window.pcm.extend(frame)
        window.voiced_frames += int(voiced)
        window.silent_frames = 0 if voiced else window.silent_frames + 1
        self._clock_samples += self.frame_samples
        if voiced:
            window.speech_revision += 1
            window.last_voiced_end = self._clock_samples
            # A short pause was not an endpoint. Keep all PCM, but never let
            # its speculative decode become a final or a stability observation.
            window.early_words = None
            window.early_words_offset = None
            window.early_words_revision = -1
        if window.silent_frames >= self.silence_frames:
            window.ended = True
            self._active = None
            self._prefix.extend(bytes(window.pcm[index:index + self.frame_bytes])
                                for index in range(max(0, len(window.pcm) - self.prefix_frames * self.frame_bytes),
                                                   len(window.pcm), self.frame_bytes))

    def snapshot(self) -> ASRSnapshot | None:
        if self._outstanding is not None or not self._windows:
            return None
        window = self._windows[0]
        end = window.offset_samples + len(window.pcm) // 2
        previous_end = window.last_snapshot_end if window.last_snapshot_end is not None else window.offset_samples
        speculative = (self.early_transcription and not window.ended
                       and window.silent_frames >= self.early_silence_frames)
        if speculative and window.early_revision == window.speech_revision:
            return None  # One speculative decode per uninterrupted pause.
        if not window.ended and not speculative and end - previous_end < self.update_samples:
            return None
        cached_words = None
        if (self.early_transcription and window.ended and window.early_words
                and window.early_words_revision == window.speech_revision
                and window.early_words_offset == window.offset_samples):
            cached_words = window.early_words
        self._sequence += 1
        snapshot = ASRSnapshot(window.identifier, self._sequence, bytes(window.pcm),
                               window.offset_samples / self.sample_rate, window.ended, window.voiced_frames,
                               bool(window.context_words), speculative, cached_words,
                               window.speech_revision, window.last_voiced_end)
        if speculative:
            window.early_revision = window.speech_revision
        else:
            # Speculation must not postpone the next regular observation when
            # speech resumes: it never counted toward the stable transcript.
            window.last_snapshot_end = end
        self._outstanding = snapshot
        return snapshot

    def _words(self, snapshot: ASRSnapshot, words) -> tuple[ASRWord, ...]:
        duration = len(snapshot.pcm) / (2 * self.sample_rate)
        result = []
        previous_end = 0.0
        for word in words:
            try:
                start, end, text = float(word.start), float(word.end), word.text
            except (AttributeError, TypeError, ValueError) as exc:
                raise EngineError("asr_timestamps", "음성 인식 단어 시간 정보가 올바르지 않습니다.", 503) from exc
            if (not isinstance(text, str) or not math.isfinite(start) or not math.isfinite(end)
                    or start < -.02 or end < start or end > duration + .25 or end + .02 < previous_end):
                raise EngineError("asr_timestamps", "음성 인식 단어 시간 정보가 올바르지 않습니다.", 503)
            previous_end = max(previous_end, end)
            if text.strip():
                result.append(ASRWord(snapshot.offset_seconds + max(0.0, min(start, duration)),
                                      snapshot.offset_seconds + max(0.0, min(end, duration)), text))
        return tuple(result)

    def _without_context(self, window: _Window, words: tuple[ASRWord, ...]) -> tuple[ASRWord, ...]:
        """Exclude published audio context, never a matching later occurrence.

        Text alignment handles CJK word-token splits without guessing character
        times. If the recognizer revises old text, absolute word times distinguish
        published context from later speech. A genuinely ambiguous crossing word
        is retained rather than deleting potentially new speech by text suffix.
        """
        if not window.context_words:
            return words
        frontier = window.context_frontier
        expected = _agreement_key("".join(word.text for word in window.context_words))
        actual = _agreement_key("".join(word.text for word in words))
        if expected and actual.startswith(expected):
            remaining = len(expected)
            valid = True
            for index, word in enumerate(words):
                key = _agreement_key(word.text)
                # The same sentence spoken again after the published frontier
                # must survive even if ASR omitted its earlier occurrence.
                if word.start >= frontier:
                    valid = False
                    break
                if len(key) <= remaining:
                    if word.end > frontier + self.context_timing_tolerance:
                        valid = False
                        break
                    remaining -= len(key)
                    if not remaining:
                        return words[index + 1:]
                    continue
                # The last published characters and fresh characters may now
                # share one ASRWord. Locate an exact original-text cut instead
                # of dropping that whole word or estimating characters/second.
                if word.end <= frontier:
                    # Extra text wholly inside already published audio is an
                    # old-context revision, not a zero-duration new suffix.
                    return words[index + 1:]
                wanted = key[:remaining]
                cut = None
                for position in range(1, len(word.text) + 1):
                    prefix = _agreement_key(word.text[:position])
                    if prefix == wanted:
                        cut = position
                    elif len(prefix) > remaining:
                        break
                if cut is not None:
                    suffix = word.text[cut:]
                    if suffix.strip():
                        start = min(word.end, max(word.start, frontier))
                        return (ASRWord(start, word.end, suffix),) + words[index + 1:]
                    return words[index + 1:]
                valid = False
                break
            if valid and not remaining:
                return ()
        result = []
        for index, word in enumerate(words):
            if word.start >= frontier:
                result.append(word)
            elif word.end <= frontier:
                continue
            elif self._published_tail_revision(window, words, index):
                continue
            else:
                # Revised text straddles the time frontier. Keeping the complete
                # timed word is conservative: no possibly new suffix disappears.
                result.append(ASRWord(frontier, word.end, word.text))
        return tuple(result)

    def _published_tail_revision(self, window: _Window, words: tuple[ASRWord, ...], index: int) -> bool:
        """Recognize a re-timed old last word using its preceding audio anchor.

        A bare overlap is insufficient: a genuinely new short response may start
        just before the frontier. Require a matching preceding word at the same
        audio time and a matching old-word start. Longer merged new suffixes stay.
        """
        if index == 0 or len(window.context_words) < 2:
            return False
        word, anchor = words[index], words[index - 1]
        old, old_anchor = window.context_words[-1], window.context_words[-2]
        tolerance = self.context_timing_tolerance
        if (abs(word.start - old.start) > tolerance
                or _agreement_key(anchor.text) != _agreement_key(old_anchor.text)
                or abs(anchor.start - old_anchor.start) > tolerance
                or abs(anchor.end - old_anchor.end) > tolerance):
            return False
        same_word = _agreement_key(word.text) == _agreement_key(old.text)
        allowed_end = .6 if same_word else tolerance
        return (len(_agreement_key(word.text)) <= len(_agreement_key(old.text))
                and word.end <= window.context_frontier + allowed_end)

    def _trim_old_context(self, window: _Window) -> None:
        if (not window.context_words or len(window.pcm) < self.context_trim_seconds * self.sample_rate * 2):
            return
        limit = window.context_frontier - self.retained_context_seconds
        boundary = 0
        for index, word in enumerate(window.context_words):
            end = max(item.end for item in window.context_words[:index + 1])
            if end > limit:
                break
            following = window.context_words[index + 1:] + window.previous_words
            if end <= min((item.start for item in following), default=math.inf):
                boundary = index + 1
        if boundary:
            end_sample = round(max(word.end for word in window.context_words[:boundary]) * self.sample_rate)
        else:
            # A long segment-level ASRWord may contain no internal time boundary.
            # Its already published audio can still be trimmed while retaining
            # three seconds of context; unpublished audio is never crossed.
            end_sample = round(limit * self.sample_rate)
        self._cut_audio(window, end_sample)

    def _cut_audio(self, window: _Window, end_sample: int) -> None:
        cut = max(0, min(len(window.pcm) // 2, end_sample - window.offset_samples))
        del window.pcm[:cut * 2]
        window.offset_samples += cut
        offset = window.offset_samples / self.sample_rate
        window.context_words = tuple(ASRWord(max(word.start, offset), word.end, word.text)
                                     for word in window.context_words if word.end > offset)
        if not window.context_words:
            window.context_frontier = None

    def _trim_idle_audio(self, window: _Window, snapshot: ASRSnapshot) -> bool:
        if (window.pending_words or window.empty_observations < 2
                or len(window.pcm) < self.deadline_seconds * self.sample_rate * 2):
            return False
        end = snapshot.offset_seconds + len(snapshot.pcm) / (2 * self.sample_rate)
        before = window.offset_samples
        self._cut_audio(window, round((end - self.retained_context_seconds) * self.sample_rate))
        if window.offset_samples == before:
            return False
        window.previous_words = ()
        if not window.context_words:
            window.had_recognized_words = False
        return True

    def _boundaries(self, words: tuple[ASRWord, ...]) -> tuple[list[int], str, list[int]]:
        """Find linguistic boundaries that coincide with whole timed words.

        Inspect the full hypothesis so a decimal, abbreviation or ellipsis is
        not mistaken for a sentence merely because the stable prefix ends there.
        """
        text = ""
        word_ends = []
        for word in words:
            text = _append_text(text, word.text)
            word_ends.append(len(text))
        sentence_ends = set()
        consumed = 0
        while prefix := completed_sentence_prefix(text[consumed:]):
            consumed += len(prefix)
            sentence_ends.add(consumed)
        boundaries = []
        phrase_start = 0
        latest_end = -math.inf
        for index, word in enumerate(words):
            latest_end = max(latest_end, word.end)
            end = len(text[:word_ends[index]].rstrip())
            phrase = text[phrase_start:end].strip()
            long_enough = len(phrase) >= self.phrase_min_characters
            clause = (long_enough and text[end - 1:end] in {",", "，", ";", "；"}
                      and not (text[end - 2:end - 1].isdigit() and text[end:end + 1].isdigit()))
            gap = words[index + 1].start - latest_end if index + 1 < len(words) else 0
            pause = ((long_enough and gap >= self.phrase_pause_seconds)
                     or (len(phrase) >= 2 and gap >= self.short_phrase_pause_seconds))
            cjk_space = (len(phrase) >= self.cjk_space_min_characters and _cjk_space_at(text, end))
            if end in sentence_ends or clause or pause or cjk_space:
                boundaries.append(index + 1)
                phrase_start = word_ends[index]
        return boundaries, text, word_ends

    def _caption_parts(self, text: str) -> list[str]:
        """Also split long silence finals that lack sentence punctuation.

        Final text may use a single segment-level timing for several phrases.
        Text splitting is safe here: the whole timed segment is being published,
        so this does not introduce a partial-word PCM trim boundary.
        """
        result = []
        for sentence in split_sentences(text):
            start = 0
            for space in re.finditer(r"\s+", sentence):
                phrase = sentence[start:space.start()].strip()
                if (len(phrase) >= self.cjk_space_min_characters
                        and _cjk_space_at(sentence, space.start())):
                    result.append(phrase)
                    start = space.end()
            tail = sentence[start:].strip()
            if tail:
                result.append(tail)
        return result

    @staticmethod
    def _safe_boundary(words: tuple[ASRWord, ...], boundary: int, offset_samples: int) -> bool:
        committed_end = max(word.end for word in words[:boundary])
        remaining_start = min((word.start for word in words[boundary:]), default=math.inf)
        return (committed_end <= remaining_start
                and round(committed_end * LocalWhisperStreaming.sample_rate) > offset_samples)

    @staticmethod
    def _splits_numeric_token(left: str, right: str) -> bool:
        """Avoid fallback cuts inside common products, numbers and units."""
        left, right = left.rstrip(), right.lstrip()
        if not left or not right:
            return False
        if re.search(r"\d[.,:，]$", left) and right[0].isdigit():
            return True
        left_ascii = re.search(r"[A-Za-z0-9]+$", left)
        right_ascii = re.match(r"[A-Za-z0-9]+", right)
        if (left_ascii and right_ascii
                and any(character.isdigit() for character in left_ascii[0] + right_ascii[0])):
            return True
        return bool(left[-1].isdigit() and re.match(
            r"[%％元块塊秒年月日个個台倍万萬亿億]|美元|日元|人民币|人民幣|"
            r"分钟|分鐘|小时|小時|公里|公斤|毫秒|원|달러|초|분|시간|개", right))

    def _fallback_boundary(self, snapshot: ASRSnapshot, words: tuple[ASRWord, ...], stable_count: int,
                           offset_samples: int, text: str, word_ends: list[int]) -> int:
        if not stable_count:
            return 0
        start = words[0].start
        received_end = snapshot.offset_seconds + len(snapshot.pcm) / (2 * self.sample_rate)
        if received_end - start < self.fallback_wait_seconds:
            return 0
        candidates = []
        for boundary in range(1, stable_count + 1):
            duration = max(word.end for word in words[:boundary]) - start
            if duration <= 0:
                continue
            end = word_ends[boundary - 1]
            # A stable middle fragment is not a complete short utterance. Allow
            # a brief whole hypothesis only after its trailing audio pause;
            # otherwise preserve it for revision instead of publishing a glyph.
            whole_utterance = (boundary == stable_count == len(words)
                               and received_end - max(word.end for word in words) >= self.short_phrase_end_seconds)
            if duration < self.fallback_minimum_seconds and not whole_utterance:
                continue
            if (self._safe_boundary(words, boundary, offset_samples)
                    and not self._splits_numeric_token(text[:end], text[end:])):
                preferred = self.fallback_minimum_seconds <= duration <= self.fallback_maximum_seconds
                candidates.append((not preferred, abs(duration - self.fallback_target_seconds), boundary))
        return min(candidates)[2] if candidates else 0

    def _deadline_boundary(self, snapshot: ASRSnapshot, words: tuple[ASRWord, ...],
                           offset_samples: int, text: str, word_ends: list[int]) -> int:
        safe_end = snapshot.offset_seconds + len(snapshot.pcm) / (2 * self.sample_rate) - self.deadline_tail_seconds
        for boundary in range(len(words), 0, -1):
            end = word_ends[boundary - 1]
            if (max(word.end for word in words[:boundary]) <= safe_end
                    and self._safe_boundary(words, boundary, offset_samples)
                    and not self._splits_numeric_token(text[:end], text[end:])):
                return boundary
        return 0

    def _deadline_due(self, snapshot: ASRSnapshot, words: tuple[ASRWord, ...], window: _Window) -> bool:
        end = snapshot.offset_seconds + len(snapshot.pcm) / (2 * self.sample_rate)
        # Retained, already published context is not recognition delay. Count
        # word age or time without publication: ASR can keep moving the latest
        # word's timestamps forward without ever stabilizing the transcription.
        progress = max(window.offset_samples / self.sample_rate, window.last_publication_audio_end or 0.0)
        return bool(words and (end - words[0].start >= self.deadline_seconds
                               or end - progress >= self.deadline_seconds))

    def _final_boundaries(self, words: tuple[ASRWord, ...], natural: list[int],
                          text: str, word_ends: list[int]) -> list[int]:
        """Split fully recognized final text without changing PCM decisions.

        Natural phrases remain preferred. A long punctuationless final is
        divided at whole timed words around three seconds. A single long timed
        word cannot be divided without inventing timing, so it stays intact.
        """
        result = []
        first = 0
        while first < len(words):
            limit = next((boundary for boundary in natural if boundary > first), len(words))
            start = words[first].start
            duration = max(word.end for word in words[first:limit]) - start
            if duration <= self.fallback_wait_seconds:
                chosen = limit
            else:
                candidates = []
                for boundary in range(first + 1, limit + 1):
                    duration = max(word.end for word in words[first:boundary]) - start
                    end = word_ends[boundary - 1]
                    if (duration >= self.fallback_minimum_seconds
                            and not self._splits_numeric_token(text[:end], text[end:])):
                        candidates.append((abs(duration - self.fallback_target_seconds), boundary))
                chosen = min(candidates)[1] if candidates else limit
            result.append(chosen)
            first = chosen
        return result

    def accept(self, snapshot: ASRSnapshot, words) -> StreamingResult:
        if snapshot is not self._outstanding or not self._windows or snapshot.window_id != self._windows[0].identifier:
            raise EngineError("local_streaming_snapshot", "지난 음성 인식 결과를 현재 세션에 적용할 수 없습니다.", 409)
        window = self._windows[0]
        if snapshot.speculative:
            self._outstanding = None
            if (window.speech_revision == snapshot.speech_revision
                    and window.offset_samples == round(snapshot.offset_seconds * self.sample_rate)):
                # Cache validated, relative timestamps. A later final snapshot
                # may contain more silence but has exactly the same speech and
                # PCM start. Empty results deliberately require a final retry.
                decoded = self._words(snapshot, words)
                if decoded:
                    window.early_words = tuple(
                        ASRWord(word.start - snapshot.offset_seconds,
                                word.end - snapshot.offset_seconds, word.text)
                        for word in decoded)
                    window.early_words_offset = window.offset_samples
                    window.early_words_revision = window.speech_revision
            # Even if the window ended while inference ran, expose its final
            # through snapshot() once, with cached_words, so consumers see an
            # authoritative final snapshot rather than a speculative commit.
            return StreamingResult((), "", "")
        decoded = self._words(snapshot, words)
        current = self._without_context(window, decoded)
        observed_empty = not current
        warnings = []
        # Truly empty decoder responses can temporarily lose real speech. Keep
        # that recovery path. Two fresh, nonempty hypotheses containing only old
        # context instead retract the unsupported pending tail; do not resurrect
        # it unconditionally at the deadline or the following silence final.
        if window.pending_words and decoded and not current:
            window.pending_context_misses += 1
            if window.pending_context_misses >= 2:
                window.pending_words = ()
                window.previous_words = ()
                window.pending_recovered = False
                warnings.append("local_streaming_pending_revised")
        else:
            window.pending_context_misses = 0
        reset_prompt = False
        used_deadline = False
        recovered_prefix_count = 0
        if current and (window.empty_observations or window.pending_recovered):
            preserved = []
            for word in window.pending_words:
                if word.end <= current[0].start:
                    preserved.append(word)
                else:
                    break
            if preserved:
                # A later, nonoverlapping utterance cannot revise an older
                # pending one that vanished during empty decoder responses.
                current = tuple(preserved) + current
                recovered_prefix_count = len(preserved)
                used_deadline = True
        # Keep the latest observed pending words through temporary empty ASR
        # responses. They do not count as a fresh stability observation.
        if (not current and window.pending_words
                and (snapshot.is_final or (not decoded and self._deadline_due(snapshot, window.pending_words, window)))):
            current = window.pending_words
            used_deadline = True
            recovered_prefix_count = len(current)
        used_fallback = False
        if snapshot.is_final:
            if (window.had_recognized_words and not window.pending_words
                    and snapshot.voiced_frames == window.previous_voiced_frames):
                # Discard inventions located only in newly appended silence.
                # A newly recognized word inside earlier audio may be delayed
                # recognition, and must not be discarded with that silence.
                previous_end = window.previous_audio_end
                if previous_end is not None:
                    current = tuple(word for word in current if word.start < previous_end)
            if (observed_empty and not window.pending_words and window.had_recognized_words
                    and snapshot.voiced_frames != window.previous_voiced_frames):
                # RMS energy alone may be background music. Keep the session
                # running while making the missing recognition observable.
                warnings.append("local_streaming_empty_final")
            count = len(current)
        else:
            previous_key = _agreement_key("".join(word.text for word in window.previous_words))
            current_key = _agreement_key("".join(word.text for word in current))
            common = 0
            for left, right in zip(previous_key, current_key):
                if left != right:
                    break
                common += 1
            safe_end = snapshot.offset_seconds + len(snapshot.pcm) / (2 * self.sample_rate) - self.end_guard_seconds
            count = key_length = 0
            for word in current:
                key_length += len(_agreement_key(word.text))
                if key_length > common or word.end > safe_end:
                    break
                count += 1
        boundaries, text, word_ends = self._boundaries(current)
        if len(text) > 6000:
            raise EngineError("transcript_too_long", "발화에 문장 경계가 너무 오래 나타나지 않아 중지했습니다.", 409)
        if not snapshot.is_final:
            # Stable middle-of-sentence words remain revisable with their PCM.
            # Only a published sentence/clause is removed from the audio window.
            stable_count = count
            candidates = []
            phrase_start = current[0].start if current else 0.0
            for boundary in boundaries:
                if boundary > stable_count:
                    break
                phrase_end = max(word.end for word in current[:boundary])
                # A distant punctuation mark must not turn 15 seconds of
                # otherwise stable speech into one delayed translation job.
                if phrase_end - phrase_start > self.fallback_wait_seconds:
                    break
                candidates.append(boundary)
                if boundary < len(current):
                    phrase_start = current[boundary].start
            count = 0
            for boundary in reversed(candidates):
                if self._safe_boundary(current, boundary, window.offset_samples):
                    count = boundary
                    break
            if not count:
                # Bounded-latency fallback: keep two-observation agreement and
                # commit a whole ~3-second phrase, never arbitrary fresh words.
                count = self._fallback_boundary(snapshot, current, stable_count,
                                                window.offset_samples, text, word_ends)
                used_fallback = bool(count)
            if not count:
                # A distant natural boundary is still progress if no shorter
                # safe word boundary exists (for example segment-level timing).
                for boundary in reversed(boundaries):
                    if boundary <= stable_count and self._safe_boundary(current, boundary, window.offset_samples):
                        count = boundary
                        break
            if not count and self._deadline_due(snapshot, current, window):
                count = self._deadline_boundary(snapshot, current, window.offset_samples, text, word_ends)
                if count:
                    used_fallback = used_deadline = True
            if not count and len(current) == 1 and len(window.pending_words) == 1:
                end = snapshot.offset_seconds + len(snapshot.pcm) / (2 * self.sample_rate)
                word = current[0]
                if (end - word.start >= self.segment_deadline_seconds
                        and abs(window.pending_words[0].start - word.start) <= .5
                        and word.end <= end
                        and self._safe_boundary(current, 1, window.offset_samples)):
                    # Segment-only timing has no internal whole-word boundary.
                    # At the hard deadline publish that latest complete timed
                    # segment and advance through its end; all later PCM stays.
                    count = 1
                    used_deadline = True
                    used_fallback = False
        committed = current[:count]
        unconfirmed = current[len(committed):]
        segments: list[str] = []
        published = (self._final_boundaries(committed, [boundary for boundary in boundaries if boundary <= count],
                                            text, word_ends) if snapshot.is_final or used_deadline and count
                     else [boundary for boundary in boundaries if boundary <= count])
        if count and (not published or published[-1] != count):
            published.append(count)  # A silence final may contain an unfinished tail.
        start = 0
        for boundary in published:
            end = word_ends[boundary - 1]
            segments.extend(self._caption_parts(text[start:end]))
            start = end
        if committed and used_fallback:
            # Publish a stable short phrase while preserving its encoder audio
            # context. Future snapshots identify and remove only this old prefix.
            window.context_words += committed
            window.context_frontier = max(word.end for word in window.context_words)
        elif committed:
            # Trim only through the end of complete, committed timed words.
            # No text-suffix deduplication: another spoken identical word stays.
            end_sample = round(max(word.end for word in committed) * self.sample_rate)
            cut = max(0, min(len(window.pcm) // 2, end_sample - window.offset_samples))
            del window.pcm[:cut * 2]
            window.offset_samples += cut
            window.context_words = ()
            window.context_frontier = None
        window.previous_words = () if observed_empty else unconfirmed
        if current:
            window.pending_words = unconfirmed
            window.pending_recovered = recovered_prefix_count > count
        window.empty_observations = window.empty_observations + 1 if observed_empty else 0
        window.previous_voiced_frames = snapshot.voiced_frames
        window.previous_audio_end = snapshot.offset_seconds + len(snapshot.pcm) / (2 * self.sample_rate)
        if count:
            window.last_publication_audio_end = window.previous_audio_end
        window.had_recognized_words = window.had_recognized_words or bool(current)
        if not snapshot.is_final:
            self._trim_old_context(window)
            reset_prompt = self._trim_idle_audio(window, snapshot)
        self._outstanding = None
        committed_text = text[:word_ends[count - 1]].strip() if count else ""
        pending_text = text[word_ends[count - 1]:].strip() if count else text.strip()
        if not pending_text and window.pending_words:
            pending_text = "".join(word.text for word in window.pending_words).strip()
        if used_deadline and count:
            warnings.append("local_streaming_deadline")
        if snapshot.is_final:
            self._windows.popleft()
        elif len(window.pcm) >= self.max_buffer_bytes:
            raise EngineError("local_streaming_overrun",
                              "연속 발화의 인식 결과가 안정되지 않아 버퍼 한도에 도달했습니다. 누락을 막기 위해 중지했습니다.", 409)
        return StreamingResult(tuple(segments), committed_text, pending_text, tuple(dict.fromkeys(warnings)), reset_prompt)
