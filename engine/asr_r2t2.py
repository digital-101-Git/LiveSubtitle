"""NetEase Confucius4-R2T2 with the bundled native Transformers CUDA runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from .asr_qwen import MAX_NEW_TOKENS, clear_cuda_cache
from .asr_warmup import SAMPLE_RATE, WARMUP_TOKENS
from .settings import EngineError, normalize_language


# The original checkpoint uses the pre-Transformers-integration Qwen3-ASR names.
# Correspondence: huggingface/transformers, models/qwen3_asr/convert_qwen3_asr_to_hf.py.
# Rename while loading so only the original tensors need to be stored on disk.
_WEIGHTS = {
    r"^thinker\.audio_tower\.proj1\.": "model.multi_modal_projector.linear_1.",
    r"^thinker\.audio_tower\.proj2\.": "model.multi_modal_projector.linear_2.",
    r"^thinker\.audio_tower\.": "model.audio_tower.",
    r"^thinker\.model\.": "model.language_model.",
    r"^thinker\.lm_head\.": "lm_head.",
}


def _native_config(path):
    from transformers import Qwen3ASRConfig

    raw = json.loads((path / "config.json").read_text(encoding="utf-8"))["thinker_config"]
    audio_fields = ("activation_dropout", "activation_function", "attention_dropout",
                    "conv_chunksize", "d_model", "downsample_hidden_size", "dropout",
                    "encoder_attention_heads", "encoder_ffn_dim", "encoder_layers",
                    "n_window", "n_window_infer", "num_mel_bins", "output_dim", "scale_embedding")
    text_fields = ("attention_bias", "attention_dropout", "head_dim", "hidden_act", "hidden_size",
                   "intermediate_size", "max_position_embeddings", "num_attention_heads",
                   "num_hidden_layers", "num_key_value_heads", "rms_norm_eps", "tie_word_embeddings",
                   "vocab_size")
    a = raw["audio_config"]
    t = raw["text_config"]
    audio = {key: a[key] for key in audio_fields}
    audio.update(num_key_value_heads=a["encoder_attention_heads"], max_position_embeddings=13)
    text = {key: t[key] for key in text_fields}
    text["rope_parameters"] = {"rope_type": "default", "rope_theta": t["rope_theta"]}
    return Qwen3ASRConfig(audio_config=audio, text_config=text, audio_token_id=raw["audio_token_id"])


@dataclass
class R2T2Stream:
    selected: str
    language: str
    pcm: bytearray = field(default_factory=bytearray)
    fixed_raw: str = ""
    closed: bool = False

    def close(self):
        self.pcm.clear()
        self.fixed_raw = ""
        self.closed = True


class R2T2ASR:
    """Greedy ASR; each partial continues the previously committed token prefix."""

    def __init__(self, path: Path):
        self.model = self.processor = self.torch = None
        try:
            import torch
            from transformers import (AutoTokenizer, Qwen3ASRFeatureExtractor,
                                      Qwen3ASRForConditionalGeneration, Qwen3ASRProcessor)
            from transformers.models.qwen3_asr.processing_qwen3_asr import LANGUAGE_CODE_TO_NAME
        except ImportError as exc:
            raise EngineError("asr_runtime_missing", "R2T2용 PyTorch CUDA와 Transformers가 필요합니다.", 409) from exc
        if not torch.cuda.is_available():
            raise EngineError("asr_gpu_missing", "R2T2에 사용할 CUDA GPU를 찾을 수 없습니다.", 503)
        self.torch = torch
        self._language_names = LANGUAGE_CODE_TO_NAME
        self._language_codes = {LANGUAGE_CODE_TO_NAME[code].casefold(): code for code in ("zh", "en", "ko", "ja")}
        try:
            tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
            # The streamed prefix is deliberately shorter than the offline processor's
            # 500 ms minimum. The source decoder expects features for the audio heard so far.
            self.processor = Qwen3ASRProcessor(Qwen3ASRFeatureExtractor(min_length=0), tokenizer)
            self.model, report = Qwen3ASRForConditionalGeneration.from_pretrained(
                str(path), config=_native_config(path), local_files_only=True, use_safetensors=True,
                dtype=torch.bfloat16, key_mapping=_WEIGHTS, output_loading_info=True,
            )
            if any(report.get(k) for k in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")):
                raise ValueError(f"R2T2 checkpoint mismatch: {report}")
            self.model.to("cuda").eval()
        except Exception as exc:
            exc.__traceback__ = None
            self.close()
            raise EngineError("asr_load_failed", "R2T2 GPU 모델을 불러오지 못했습니다. 모델 파일과 GPU 메모리를 확인하세요.", 503) from exc

    def create_stream(self, language):
        selected = normalize_language(language)
        return R2T2Stream(selected, selected)

    def _decode(self, samples, selected, prefix, limit, context=""):
        if self.model is None:
            raise EngineError("asr_not_ready", "R2T2 모델을 먼저 준비해 주세요.", 409)
        # Exact single-turn string produced by the original R2T2 checkpoint's chat_template.json.
        prompt = (f"<|im_start|>system\n{context}<|im_end|>\n"
                  "<|im_start|>user\n<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n"
                  "<|im_start|>assistant\n")
        if selected != "auto":
            prompt += f"language {self._language_names[selected]}<asr_text>"
        inputs = self.processor(text=[prompt + prefix], audio=[samples], return_tensors="pt").to(
            self.model.device, self.model.dtype)
        with self.torch.inference_mode():
            tokens = self.model.generate(**inputs, max_new_tokens=limit, do_sample=False,
                                         temperature=1.0)[0, inputs.input_ids.shape[1]:].tolist()
        eos = self.model.generation_config.eos_token_id
        eos = eos if isinstance(eos, (tuple, list)) else [eos]
        stopped = bool(tokens) and tokens[-1] in eos
        if stopped:
            tokens.pop()
        tail = self.processor.tokenizer.decode(tokens, skip_special_tokens=False)
        if limit == MAX_NEW_TOKENS and len(tokens) >= limit and "|" not in tail:
            raise EngineError("asr_truncated", "R2T2 전사 결과가 길이 제한에 도달했습니다.", 422)
        # R2T2 emits a literal vertical bar for WAIT on an incomplete acoustic prefix.
        return prefix + tail.partition("|")[0].rstrip("\ufffd")

    def _transcript(self, raw, stream, *, identify=True):
        if stream.selected != "auto":
            return raw.strip()
        header, marker, content = raw.partition("<asr_text>")
        if not marker:
            return ""
        name = header.removeprefix("language ").strip().casefold()
        text = content.strip()
        if text and identify:
            # Keep unlabeled speech usable without guessing a language from the
            # script. HY already accepts "auto", including ambiguous Han-only text.
            stream.language = self._language_codes.get(name, "auto")
        return text

    def _stable(self, raw, previous):
        tokenizer = self.processor.tokenizer
        ids = tokenizer.encode(raw, add_special_tokens=False)
        # Quality profile U=1. Withhold more bytes if necessary to avoid committing
        # a partial UTF-8 character, and never shorten an already released prefix.
        for end in range(len(ids) - 1, 0, -1):
            candidate = tokenizer.decode(ids[:end], skip_special_tokens=False)
            if "\ufffd" not in candidate:
                return candidate if candidate.startswith(previous) else previous
        return previous

    def step(self, stream, pcm, final=False):
        import numpy as np

        if stream.closed or len(pcm) % 2:
            raise EngineError("invalid_audio", "종료된 R2T2 스트림이거나 PCM 길이가 잘못되었습니다.")
        stream.pcm.extend(pcm)
        if not stream.pcm:
            if final:
                stream.close()
            return "", ""
        samples = np.frombuffer(stream.pcm, dtype="<i2").astype("float32") / 32768.0
        if samples.size < 400:
            if not final:
                return "", ""
            samples = np.pad(samples, (0, 400 - samples.size))
        limit = MAX_NEW_TOKENS if final else min(64, max(8, len(pcm) // 1280 + 4))
        raw = self._decode(samples, stream.selected, stream.fixed_raw, limit)
        full = self._transcript(raw, stream)
        if final:
            stream.close()
            return full, full
        # Silence can produce "language None<asr_text>". Prefilling that label would
        # force every later update in the utterance to retain an unknown language.
        if stream.selected == "auto" and not full:
            return full, ""
        stream.fixed_raw = self._stable(raw, stream.fixed_raw)
        return full, self._transcript(stream.fixed_raw, stream, identify=False)

    def transcribe(self, samples, language, *, prompt=""):
        from faster_whisper.vad import get_speech_timestamps

        if samples.ndim != 1 or not samples.size:
            raise EngineError("invalid_audio", "16 kHz 모노 PCM이 필요합니다.")
        state = self.create_stream(language)
        if not get_speech_timestamps(samples, sampling_rate=SAMPLE_RATE):
            return "", state.language
        raw = self._decode(samples, state.selected, "", MAX_NEW_TOKENS, prompt)
        return self._transcript(raw, state), state.language

    def warmup(self):
        import numpy as np
        self._decode(np.zeros(SAMPLE_RATE, dtype=np.float32), "en", "", WARMUP_TOKENS)
        self.torch.cuda.synchronize(self.model.device)

    def close(self):
        self.model = self.processor = None
        clear_cuda_cache()
