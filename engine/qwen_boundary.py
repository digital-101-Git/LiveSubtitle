"""Bounded re-decoding of a forced PCM boundary, without invented word times.

Natural RMS boundaries retain the existing chunker's policy. At a forced cut,
the same audio start is decoded once, then once more with up to one extra second.
No character offset is converted to an audio timestamp. A failed recheck can
return the extra samples to the next window instead of silently losing them.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import struct


@dataclass(frozen=True)
class BoundaryRequest:
    pcm: bytes
    start_sample: int
    end_sample: int
    probe: bool
    rechecked: bool
    forced: bool
    overlaps_previous: bool


class BoundaryChunker:
    frame_bytes = 640
    samples_per_frame = 320
    initial_frames = 200
    extra_frames = 50
    silence_frames = 25
    overlap_frames = 10
    prefix_frames = 20

    def __init__(self, threshold: float = .002):
        self.threshold = threshold
        self._pending = bytearray()
        self._prefix = deque(maxlen=self.prefix_frames)
        self._frames: list[tuple[int, bytes]] = []
        self._frame_index = 0
        self._silent = self._voiced = 0
        self._overlap = False
        self._checkpoint = 0
        self._request: BoundaryRequest | None = None

    @property
    def buffered_seconds(self) -> float:
        return (len(self._pending) + len(self._frames) * self.frame_bytes) / 32000

    def feed(self, pcm: bytes) -> None:
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) % 2:
            raise ValueError('Expected complete PCM16 samples.')
        # The session supplies at most one second and drains before feeding again.
        if len(self._pending) + len(pcm) > 32000 + self.frame_bytes - 2:
            raise ValueError('Drain pending audio before feeding another packet.')
        self._pending.extend(pcm)

    def next_request(self) -> BoundaryRequest | None:
        if self._request is not None:
            raise RuntimeError('Resolve the current recognition request first.')
        while len(self._pending) >= self.frame_bytes:
            frame = bytes(self._pending[:self.frame_bytes])
            del self._pending[:self.frame_bytes]
            item = (self._frame_index, frame)
            self._frame_index += 1
            samples = struct.unpack('<320h', frame)
            rms = math.sqrt(sum(value * value for value in samples) / 320) / 32768
            voiced = rms >= self.threshold
            if not self._frames:
                if not voiced:
                    self._prefix.append(item)
                    continue
                self._frames = list(self._prefix)
                self._prefix.clear()
            self._frames.append(item)
            self._voiced += int(voiced)
            self._silent = 0 if voiced else self._silent + 1
            natural = self._silent >= self.silence_frames
            limit = self.initial_frames + (self.extra_frames if self._checkpoint else 0)
            forced = len(self._frames) >= limit and not natural
            if not natural and not forced:
                continue
            if self._voiced < 10:
                self._finish(forced)
                continue
            probe = forced and not self._checkpoint
            self._request = BoundaryRequest(
                pcm=b''.join(frame for _, frame in self._frames),
                start_sample=self._frames[0][0] * self.samples_per_frame,
                end_sample=(self._frames[-1][0] + 1) * self.samples_per_frame,
                probe=probe, rechecked=bool(self._checkpoint), forced=forced,
                overlaps_previous=self._overlap)
            return self._request
        return None

    def resolve(self, request: BoundaryRequest, *, use_extension: bool = True) -> None:
        if request is not self._request:
            raise RuntimeError('The recognition result belongs to a different request.')
        if request.probe:
            self._checkpoint = len(self._frames)
        else:
            if request.rechecked and not use_extension:
                extension = self._frames[self._checkpoint:]
                self._pending[:0] = b''.join(frame for _, frame in extension)
                self._frame_index -= len(extension)
                self._frames = self._frames[:self._checkpoint]
                self._finish(True)
            else:
                self._finish(request.forced)
        self._request = None

    def _finish(self, forced: bool) -> None:
        if forced:
            self._frames = self._frames[-self.overlap_frames:]
            self._overlap = True
        else:
            self._prefix.extend(self._frames[-self.prefix_frames:])
            self._frames = []
            self._overlap = False
        self._silent = self._voiced = self._checkpoint = 0
