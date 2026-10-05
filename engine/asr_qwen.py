"""Native Transformers Qwen3-ASR, using only installed local safetensors files."""
from __future__ import annotations

import gc
import sys
from pathlib import Path

from .settings import EngineError, normalize_language
from .asr_warmup import SAMPLE_RATE, WARMUP_TOKENS


MAX_NEW_TOKENS = 256
LANGUAGE_CODES = {"english": "en", "chinese": "zh", "japanese": "ja",
                  "korean": "ko", "cantonese": "yue"}


def clear_cuda_cache() -> None:
    """Do not import/initialize PyTorch just to release an unused backend."""
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_initialized():
        torch.cuda.empty_cache()


class QwenASR:
    def __init__(self, path: Path):
        self.model = self.processor = self.torch = None
        try:
            import torch
            from transformers import AutoModelForMultimodalLM, AutoProcessor
        except ImportError as exc:
            raise EngineError("asr_runtime_missing", "Qwen3-ASR용 PyTorch CUDA와 Transformers 5.13 이상을 설치해 주세요.", 409) from exc
        self.torch = torch
        if not torch.cuda.is_available():
            raise EngineError("asr_gpu_missing", "Qwen3-ASR에 사용할 CUDA GPU를 찾지 못했습니다. GPU 드라이버와 PyTorch CUDA 설치를 확인해 주세요.", 503)
        try:
            self.processor = AutoProcessor.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
            self.model = AutoModelForMultimodalLM.from_pretrained(
                str(path), local_files_only=True, trust_remote_code=False,
                use_safetensors=True, dtype=torch.bfloat16,
            )
            self.model.to("cuda").eval()
        except Exception as exc:
            # Failed device transfers can retain partially allocated tensors in
            # traceback frames. Drop those frames before releasing the cache.
            exc.__traceback__ = None
            self.close()
            raise EngineError("asr_load_failed", "Qwen3-ASR GPU 로딩에 실패했습니다. 모델 파일, Transformers 버전, GPU 여유 메모리를 확인해 주세요.", 503) from exc

    def transcribe(self, samples, language: str | None, *, prompt: str = "") -> tuple[str, str]:
        selected = normalize_language(language)
        if self.model is None or self.processor is None:
            raise EngineError("asr_not_ready", "Qwen3-ASR 모델을 먼저 준비해 주세요.", 409)
        if samples.ndim != 1 or samples.size == 0:
            raise EngineError("invalid_audio", "음성은 비어 있지 않은 16 kHz 모노 PCM이어야 합니다.")
        from faster_whisper.vad import get_speech_timestamps
        # Reject non-speech with the already bundled local Silero model. Use VAD
        # only as a gate: preserve quiet syllables, pauses and the original waveform.
        if not get_speech_timestamps(samples, sampling_rate=16000):
            return "", selected
        return self._infer(samples, selected, prompt=prompt)

    def _generate(self, samples, selected: str, *, prompt: str = "", max_new_tokens: int = MAX_NEW_TOKENS):
        """Private inference path; the public transcribe method always applies VAD."""
        inputs = self.processor.apply_transcription_request(
            audio=samples, language=None if selected == "auto" else selected,
            processor_kwargs={"audio_kwargs": {"sampling_rate": 16000}},
            **({"prompt": prompt} if prompt else {}),
        ).to(self.model.device, self.model.dtype)
        output = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        return output[:, inputs["input_ids"].shape[1]:]

    def warmup(self) -> None:
        """Exercise local CUDA features and two decode steps, discarding all output.

        Silence deliberately bypasses the public Silero gate only here. It is
        synthetic, never stored, never parsed as a transcript and uses no hints.
        Runtime owns asr_lock throughout this call and must not unload early.
        """
        if self.model is None or self.processor is None:
            raise EngineError("asr_not_ready", "Qwen3-ASR 모델을 먼저 준비해 주세요.", 409)
        import numpy as np
        from faster_whisper.vad import get_speech_timestamps
        samples = np.zeros(SAMPLE_RATE, dtype=np.float32)
        get_speech_timestamps(samples, sampling_rate=SAMPLE_RATE)
        with self.torch.inference_mode():
            self._generate(samples, "en", max_new_tokens=WARMUP_TOKENS)
        self.torch.cuda.synchronize(self.model.device)

    def _infer(self, samples, selected: str, *, prompt: str = "") -> tuple[str, str]:
        with self.torch.inference_mode():
            # A numpy waveform goes directly to the feature extractor: no URLs,
            # temporary recordings, decoding libraries, or audio network requests.
            generated = self._generate(samples, selected, prompt=prompt)
            eos = self.model.generation_config.eos_token_id
            eos_ids = eos if isinstance(eos, (list, tuple)) else [eos]
            if generated.shape[1] >= MAX_NEW_TOKENS and int(generated[0, -1].item()) not in eos_ids:
                raise EngineError("asr_truncated", "음성 인식 결과가 출력 길이 제한에 도달했습니다. 짧은 구간으로 다시 시도해 주세요.", 422)
            parsed = self.processor.decode(generated, return_format="parsed")
        if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
            raise EngineError("asr_invalid_output", "Qwen3-ASR 음성 인식 결과 형식이 올바르지 않습니다.", 503)
        transcription = parsed[0].get("transcription")
        if not isinstance(transcription, str):
            raise EngineError("asr_invalid_output", "Qwen3-ASR에서 원문 텍스트를 받지 못했습니다.", 503)
        detected = parsed[0].get("language")
        if selected != "auto":
            source_language = selected
        elif isinstance(detected, str) and detected.strip():
            source_language = LANGUAGE_CODES.get(detected.strip().lower(), detected.strip().lower())
        else:
            source_language = "auto"
        return transcription.strip(), source_language

    def close(self) -> None:
        self.model = self.processor = None
        clear_cuda_cache()
