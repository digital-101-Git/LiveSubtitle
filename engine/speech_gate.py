"""Stateful Silero-v6 activity hints; input PCM is never removed or rewritten.

The bundled faster-whisper ONNX session is shared; recurrent states and the
64-sample context belong to each stream. This matches that runtime's input/h/c
contract. The callable accepts short PCM packets and returns the latest decision.
It does not split audio, infer timestamps, or own a GPU model.
"""
from __future__ import annotations

import math
import numpy as np

from .settings import EngineError


class OnlineSpeechGate:
    sample_rate = 16000
    window_samples = 512
    context_samples = 64

    def __init__(self, session=None, *, threshold: float = .5,
                 exit_threshold: float = .35, rms_floor: float = .002):
        if not (0 <= exit_threshold <= threshold <= 1) or not (0 <= rms_floor <= 1):
            raise ValueError("Invalid speech probability/RMS thresholds.")
        if session is None:
            try:
                from faster_whisper.vad import get_vad_model
                session = get_vad_model().session
            except Exception as exc:
                raise EngineError("speech_gate_failed", "로컬 음성 활동 감지기를 준비하지 못했습니다.") from exc
        self.session = session
        # Compare at the model's float32 precision, including the exact .35
        # boundary, rather than accidentally changing it during float casting.
        self.threshold = float(np.float32(threshold))
        self.exit_threshold = float(np.float32(exit_threshold))
        self.rms_floor = rms_floor
        self.reset()

    def reset(self) -> None:
        self._pending = bytearray()
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.context_samples), dtype=np.float32)
        self.probability = 0.0
        self.speaking = False

    def __call__(self, pcm: bytes) -> bool:
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) % 2:
            raise ValueError("Expected complete PCM16 samples.")
        if len(pcm) > self.sample_rate * 2:
            raise ValueError("Feed at most one second of PCM at a time.")
        if not pcm:
            return self.speaking
        current = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        rms = float(np.sqrt(np.mean(current * current)))
        self._pending.extend(pcm)
        size = self.window_samples * 2
        while len(self._pending) >= size:
            raw = bytes(self._pending[:size])
            del self._pending[:size]
            samples = np.frombuffer(raw, dtype='<i2').astype(np.float32).reshape(1, -1) / 32768
            model_input = np.concatenate((self._context, samples), axis=1)
            try:
                probability, h, c = self.session.run(None, {"input": model_input, "h": self._h, "c": self._c})
                probability = float(np.asarray(probability).reshape(-1)[-1])
                h = np.asarray(h, dtype=np.float32)
                c = np.asarray(c, dtype=np.float32)
                if not math.isfinite(probability) or not 0 <= probability <= 1 or h.shape != self._h.shape or c.shape != self._c.shape:
                    raise ValueError("Invalid activity detector output.")
            except Exception as exc:
                raise EngineError("speech_gate_failed", "로컬 음성 활동 감지 중 오류가 발생했습니다.") from exc
            self.probability = probability
            self._h, self._c = h, c
            self._context = samples[:, -self.context_samples:].copy()
            if probability >= self.threshold:
                self.speaking = True
            elif probability < self.exit_threshold:
                self.speaking = False
        if rms < self.rms_floor:
            # Still process quiet PCM above so model time and recurrent state
            # remain aligned with the original continuous audio.
            self.speaking = False
        return self.speaking
