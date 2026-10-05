"""Local CT2 encoder + pinned WhisperLiveKit AlignAtt Torch decoder.

The caller serializes loading, feed and close with Runtime.asr_lock. PCM is
incremental mono PCM16/16 kHz, never an overlapping recognition snapshot.
Returned words are newly committed decoder output, not provisional hypotheses.
Attention-derived timestamps are estimates, not forced-alignment ground truth.
No model downloads, automatic CPU fallback, text deduplication or silent errors.
"""
from __future__ import annotations

from pathlib import Path
import math
from typing import Any
from weakref import WeakSet

from .local_streaming import ASRWord

TURBO_ALIGNMENT_HEADS = b"ABzY8j^C+e0{>%RARaKHP%t(lGR*)0g!tONPyhe`"
LARGE_V3_ALIGNMENT_HEADS = b"ABzY8gWO1E0{>%R7(9S+Kn!D~%ngiGaR?*L!iJG9p-nab0JQ=-{D1-g00"


def _committed_split(tokenizer, tokens, fire_detected, is_last):
    """Keep exported words consistent with the decoder's committed token IDs.

    Upstream _split_tokens excludes the held final word from hypothesis but
    leaves it in split_words. Exporting that word would expose an unstable tail
    and repeat it on the next decode. CJK uses Unicode units, not spaces.
    """
    words, groups = tokenizer.split_to_word_tokens(tokens)
    words, groups = list(words), list(groups)
    if not fire_detected and not is_last:
        words, groups = words[:-1], groups[:-1]
    # Unfinished UTF-8 tokens are not committed; next decode can regenerate
    # them from the same retained audio and committed token prefix.
    while words and "\ufffd" in words[-1]:
        words.pop()
        groups.pop()
    return [t for group in groups for t in group], words, groups


def _decoder_class():
    from ._vendor.whisperlivekit.simul_whisper.simul_whisper import AlignAtt

    class CommittedAlignAtt(AlignAtt):
        def _split_tokens(self, tokens_list, fire_detected, is_last):
            return _committed_split(self.tokenizer, tokens_list, fire_detected, is_last)

        def _detect_language_if_needed(self, encoder_feature):
            # Upstream waits until AFTER emitting initial language-less text.
            # Our stream buffers the first 2 s in auto mode, then identifies
            # before committing any text. A short final utterance is still read.
            if self.cfg.language == "auto" and self.state.detected_language is None:
                _, probabilities = self.lang_id(encoder_feature)
                language = max(probabilities[0], key=probabilities[0].get)
                self.create_tokenizer(language)
                self.state.detected_language = language
                self.init_tokens()
                self.init_context()

        def _apply_dry_penalty(self, logits, current_tokens):
            # A repeated utterance must remain repeatable. No text-only loop
            # suppression is layered over AlignAtt's audio attention policy.
            return logits

        def _encode(self, input_segments):
            features, length = super()._encode(input_segments)
            return features.to(dtype=self.model.decoder.token_embedding.weight.dtype), length

    return CommittedAlignAtt


def _decoder_precision(model):
    """Whisper normalizes FP32 activations even when decoder math is FP16.

    Its LayerNorm.forward calls F.layer_norm(x.float(), weight, bias) before
    converting back. Those parameters must therefore stay float32 too.
    """
    import torch
    model.eval().half()
    for layer in model.modules():
        if isinstance(layer, torch.nn.LayerNorm):
            layer.float()
    return model


def build_decoder(dimensions: dict, state_dict: dict, *, model_name="large-v3-turbo"):
    """Construct decoder directly from RAM; no checkpoint file is required.

    state_dict uses OpenAI Whisper decoder.* keys. This permits the parent
    loader to convert the already installed CT2 weights without duplicating a
    model download. A local standard Whisper checkpoint is also supported by
    AlignAttASR for callers that already possess one.
    """
    import torch
    from ._vendor.whisperlivekit.whisper.model import ModelDimensions, Whisper

    if not torch.cuda.is_available():
        raise RuntimeError("Whisper AlignAtt requires the configured CUDA GPU.")
    if model_name not in {"large-v3-turbo", "large-v3"}:
        raise ValueError("AlignAtt requires known large-v3/turbo alignment heads.")
    dims = ModelDimensions(**dimensions)
    expected_layers = 4 if model_name == "large-v3-turbo" else 32
    if dims.n_text_layer != expected_layers or dims.n_mels != 128:
        raise ValueError("Whisper decoder dimensions do not match the selected model.")
    model = Whisper(dims, decoder_only=True)
    model.load_state_dict({key: value for key, value in state_dict.items()
                           if key.startswith("decoder.")}, strict=True)
    heads = TURBO_ALIGNMENT_HEADS if model_name == "large-v3-turbo" else LARGE_V3_ALIGNMENT_HEADS
    model.set_alignment_heads(heads)
    return _decoder_precision(model).to("cuda")


class AlignAttASR:
    def __init__(self, decoder_path: Path | None = None, encoder: Any = None, *,
                 decoder_model: Any = None, decoder_state_dict: dict | None = None,
                 dimensions: dict | None = None, model_name="large-v3-turbo"):
        if encoder is None or not callable(getattr(encoder, "encode", None)):
            raise ValueError("An existing faster-whisper encoder is required.")
        if decoder_model is None:
            if decoder_state_dict is None:
                import torch
                if decoder_path is None or not Path(decoder_path).is_file():
                    raise FileNotFoundError("A local decoder or in-memory decoder weights are required.")
                checkpoint = torch.load(Path(decoder_path), map_location="cpu", weights_only=True)
                if not isinstance(checkpoint, dict):
                    raise ValueError("Invalid Whisper decoder checkpoint.")
                dimensions = checkpoint.get("dims")
                decoder_state_dict = checkpoint.get("model_state_dict")
            if not isinstance(dimensions, dict) or not isinstance(decoder_state_dict, dict):
                raise ValueError("Whisper decoder requires dimensions and a state dict.")
            decoder_model = build_decoder(dimensions, decoder_state_dict, model_name=model_name)
        self.model = decoder_model
        self.encoder = encoder  # Borrowed: close() must not unload the CT2 owner.
        self._streams: WeakSet[AlignAttStream] = WeakSet()
        self.closed = False

    def create_stream(self, language="auto"):
        if self.closed:
            raise RuntimeError("Whisper AlignAtt backend is closed.")
        if language not in {"auto", "en", "zh", "ja", "ko"}:
            raise ValueError("Unsupported AlignAtt input language.")
        from ._vendor.whisperlivekit.simul_whisper.config import AlignAttConfig
        cfg = AlignAttConfig(
            language=language, tokenizer_is_multilingual=True, task="transcribe",
            segment_length=1.0, audio_min_len=0.02, audio_max_len=20.0,
            frame_threshold=25, decoder_type="greedy", beam_size=1,
            # No CIF checkpoint: hold the last Unicode/word unit until more
            # audio or a final flush rather than pretending CIF is available.
            never_fire=True, max_context_tokens=224,
        )
        decoder = _decoder_class()(cfg=cfg, loaded_model=self.model, fw_encoder=self.encoder)
        stream = AlignAttStream(decoder, language)
        self._streams.add(stream)
        return stream

    def close(self):
        if self.closed:
            return
        for stream in self._streams:
            stream.close()
        self._streams.clear()
        self.model = None
        self.encoder = None
        self.closed = True


class AlignAttStream:
    def __init__(self, decoder, language="auto"):
        self.decoder = decoder
        self.language = language
        self._pending = bytearray()
        self._total_samples = 0
        self._segment_samples = 0
        self.closed = False

    def feed(self, pcm: bytes, final=False) -> tuple[list[ASRWord], str]:
        if self.closed:
            raise RuntimeError("Whisper AlignAtt stream is closed.")
        if not isinstance(pcm, bytes) or len(pcm) % 2:
            raise ValueError("AlignAtt expects PCM16 bytes with whole samples.")
        if len(pcm) > 20 * 32000:
            raise ValueError("Feed incremental audio, not a full recording.")
        self._pending.extend(pcm)
        self._total_samples += len(pcm) // 2
        self._segment_samples += len(pcm) // 2
        minimum = 64000 if self.language == "auto" else 32000
        if not final and len(self._pending) < minimum:
            return [], self.language
        if not self._segment_samples:
            return [], self.language
        import numpy as np
        import torch
        try:
            if self._pending:
                audio = np.frombuffer(bytes(self._pending), dtype="<i2").astype(np.float32) / 32768.0
                self._pending.clear()
                if final:
                    audio = np.concatenate((audio, np.zeros(3200, dtype=np.float32)))
                self.decoder.insert_audio(torch.from_numpy(audio))
            elif final:
                # A short zero tail gives right-edge attention room on stop.
                self.decoder.insert_audio(torch.zeros(3200))
            tokens = self.decoder.infer(is_last=bool(final))
            detected = getattr(self.decoder.state, "detected_language", None)
            if detected:
                self.language = detected
            words = []
            audio_end = self._total_samples / 16000.0
            for token in tokens or []:
                start, end = float(token.start), float(token.end)
                if not math.isfinite(start) or not math.isfinite(end) or end < start:
                    raise RuntimeError("AlignAtt returned invalid token timestamps.")
                if token.text:
                    # Ends include upstream's estimated final-token duration.
                    # Keep text/repetitions, constrain metadata to received audio.
                    start = max(0.0, min(start, audio_end))
                    end = max(start, min(end, audio_end))
                    words.append(ASRWord(start, end, token.text))
            if final:
                self.decoder.refresh_segment(complete=True)
                self.decoder.global_time_offset = audio_end
                self._segment_samples = 0
            return words, self.language
        except Exception:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self._pending.clear()
        if self.decoder is not None:
            self.decoder.refresh_segment(complete=True)
        self.decoder = None
        self.closed = True
