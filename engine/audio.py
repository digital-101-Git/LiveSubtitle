"""Small PCM queues and conservative utterance segmentation, independent of ML."""
from __future__ import annotations

import asyncio
import math
import struct
import unicodedata
from collections import deque
from dataclasses import dataclass


class AudioQueue:
    """Bounded by both bytes (six seconds) and item count, regardless of frame size."""
    def __init__(self, max_bytes: int = 192000, max_items: int = 120):
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(max_items)
        self.max_bytes = max_bytes
        self.bytes = 0

    def put_nowait(self, value: bytes) -> None:
        if self.bytes + len(value) > self.max_bytes:
            raise asyncio.QueueFull
        self.queue.put_nowait(value)
        self.bytes += len(value)

    async def get(self) -> bytes:
        item = await self.queue.get()
        self.bytes -= len(item)
        return item


@dataclass
class Utterance:
    pcm: bytes
    overlaps_previous: bool


class PCMChunker:
    """20 ms RMS frames; 400 ms prefix, 500 ms silence, at most 4 s per chunk.

    RMS is only a cheap input gate. Whisper's bundled Silero VAD performs the
    speech-specific filtering. Forced cuts preserve 200 ms of audio context.
    """
    frame_bytes = 640
    prefix_frames = 20

    def __init__(self, threshold: float = 0.002):
        self.threshold = threshold
        self.pending = bytearray()
        self.prefix: deque[bytes] = deque(maxlen=self.prefix_frames)
        self.frames: list[bytes] = []
        self.silent = 0
        self.voiced = 0
        self.overlap = False

    def feed(self, data: bytes) -> list[Utterance]:
        self.pending.extend(data)
        emitted = []
        while len(self.pending) >= self.frame_bytes:
            frame = bytes(self.pending[:self.frame_bytes])
            del self.pending[:self.frame_bytes]
            samples = struct.unpack("<320h", frame)
            rms = math.sqrt(sum(value * value for value in samples) / 320) / 32768
            voiced = rms >= self.threshold
            if not self.frames:
                if not voiced:
                    self.prefix.append(frame)
                    continue
                self.frames = list(self.prefix)
                self.prefix.clear()
            self.frames.append(frame)
            self.voiced += int(voiced)
            self.silent = 0 if voiced else self.silent + 1
            forced = len(self.frames) >= 200
            if self.silent >= 25 or forced:
                if self.voiced >= 10:
                    emitted.append(Utterance(b"".join(self.frames), self.overlap))
                if forced and self.silent < 25:
                    self.frames = self.frames[-10:]
                    self.voiced = 0
                    self.overlap = True
                else:
                    self.prefix.extend(self.frames[-self.prefix_frames:])
                    self.frames = []
                    self.voiced = 0
                    self.overlap = False
                self.silent = 0
        return emitted


def remove_overlap(previous: str, current: str) -> str:
    """Remove only substantial exact overlap after an audio cut, not repetitions
    across independent utterances. Match at word boundary for space languages.
    """
    previous, current = previous.strip(), current.strip()
    for size in range(min(len(previous), len(current), 100), 3, -1):
        if previous[-size:].casefold() != current[:size].casefold():
            continue
        overlap = current[:size]
        if overlap.isascii() and size < len(current) and current[size].isalnum():
            continue
        return current[size:].lstrip(" ,，.。!！?？")
    return current


@dataclass(frozen=True)
class ForcedOverlap:
    """Comparison evidence only; character positions never become audio times."""
    key: str
    previous_key: str
    current_key: str
    current_end: int


def _boundary_units(text: str, language: str):
    from .text_normalization import chinese_comparison_text
    units, ends = [], []
    for index, char in enumerate(text):
        mapped = chinese_comparison_text(char) if language == "zh" else char
        for item in mapped.casefold():
            if item.isspace() or unicodedata.category(item).startswith("P"):
                continue
            units.append(item)
            ends.append(index + 1)
    return "".join(units), ends


def _cjk(char):
    return ("\u3400" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff"
            or "\U00020000" <= char <= "\U0003134f")


def forced_overlap_candidate(previous: str, current: str, language: str = "auto") -> ForcedOverlap | None:
    """Find CJK suffix/prefix agreement without claiming it proves duplication.

    A 2-3-character candidate requires a separate audio seam observation before
    removal. Whole-current repetition is deliberately not a trimming candidate.
    """
    old, _ = _boundary_units(previous, language)
    new, ends = _boundary_units(current, language)
    for size in range(min(len(old), len(new) - 1, 100), 1, -1):
        key = new[:size]
        if old.endswith(key) and all(_cjk(char) for char in key):
            return ForcedOverlap(key, old, new, ends[size-1])
    return None


def seam_repetition(match: ForcedOverlap, seam_text: str, language: str = "auto") -> str:
    """Return single/repeated only for exact uniquely observed anchored forms.

    The caller must recognize real, concatenated audio spanning the known PCM
    overlap. This function does no fuzzy repair and never invents source words.
    """
    seam, _ = _boundary_units(seam_text, language)
    left = match.previous_key[:-len(match.key)]
    right = match.current_key[len(match.key):]
    decisions = set()
    for before in range(min(6, len(left)), 1, -1):
        for after in range(min(6, len(right)), 1, -1):
            single = left[-before:] + match.key + right[:after]
            repeated = left[-before:] + match.key * 2 + right[:after]
            one, two = seam.count(single), seam.count(repeated)
            if one == 1 and two == 0:
                decisions.add("single")
            elif two == 1 and one == 0:
                decisions.add("repeated")
            elif one or two:
                return "ambiguous"
    return decisions.pop() if len(decisions) == 1 else "ambiguous"


def remove_forced_overlap(previous: str, current: str, language: str = "auto") -> str:
    """Normalize only known forced PCM overlap, retaining the existing 4-unit floor.

    Text agreement is a heuristic, not speech timing. Independent full-sentence
    repetitions and short agreements remain intact; ordinary pause boundaries
    must never call this helper.
    """
    previous, current = previous.strip(), current.strip()
    match = forced_overlap_candidate(previous, current, language)
    if match and len(match.key) >= 4:
        # Preserve a repeated complete sentence, including a sentence followed
        # by a continuation: '你听我说。' -> '你听我说完'. The punctuation on a
        # partial ASR result is not enough to establish where its speech ends.
        stripped = previous.rstrip(" \t\r\n。.!！?？")
        last_sentence = stripped
        for mark in "。.!！?？":
            last_sentence = last_sentence.rsplit(mark, 1)[-1]
        sentence_key, _ = _boundary_units(last_sentence, language)
        repeated_pattern = any(match.key == match.key[:n] * (len(match.key)//n)
                               for n in range(1, len(match.key)//2 + 1)
                               if len(match.key) % n == 0)
        if sentence_key == match.key or repeated_pattern:
            return current
        return current[match.current_end:].lstrip(" \t\r\n,，、.。!！?？:：;；")
    # Keep legacy literal matching for spaced languages, while requiring both
    # sides of a Latin overlap to be word boundaries (not 'contest'/'test').
    for size in range(min(len(previous), len(current), 100), 3, -1):
        if previous[-size:].casefold() != current[:size].casefold():
            continue
        if size == len(current) or size == len(previous):
            return current
        overlap = current[:size]
        if any(_cjk(char) for char in overlap):
            continue  # Punctuation cannot turn 2-3 CJK units into a 4-unit match.
        if overlap.isascii() and (previous[-size-1].isalnum() or current[size].isalnum()):
            continue
        return current[size:].lstrip(" ,，.。!！?？")
    return current
