"""CPU-only contract tests: no weights, network, or actual ASR inference."""
from types import SimpleNamespace

import pytest

from engine.asr_alignatt import AlignAttASR, AlignAttStream, _committed_split


class Tokenizer:
    def __init__(self, words):
        self.words = words

    def split_to_word_tokens(self, tokens):
        return self.words, [[i] for i in tokens]


@pytest.mark.parametrize("words", [[" Hello", " there"], ["你", "好"], ["こ", "れ"]])
def test_unstable_last_unit_is_neither_committed_nor_exported(words):
    ids, output, groups = _committed_split(Tokenizer(words), [1, 2], False, False)
    assert ids == [1]
    assert output == words[:1]
    assert groups == [[1]]


def test_short_tail_waits_but_final_flush_keeps_it():
    tokenizer = Tokenizer(["嗯"])
    assert _committed_split(tokenizer, [1], False, False) == ([], [], [])
    assert _committed_split(tokenizer, [1], False, True) == ([1], ["嗯"], [[1]])


def test_incomplete_unicode_never_becomes_committed_replacement_character():
    assert _committed_split(Tokenizer(["我", "\ufffd"]), [1, 2], True, True) == ([1], ["我"], [[1]])


def test_real_repeated_words_are_not_deduplicated():
    assert _committed_split(Tokenizer(["走", "走", "走"]), [1, 2, 3], True, True)[1] == ["走"] * 3


@pytest.mark.parametrize("language,text", [("en", " Hello, hello."), ("zh", "走，走，走！"), ("ja", "ありがとうございます。")])
def test_actual_bundled_tokenizer_preserves_unicode_and_repetition(language, text):
    from engine._vendor.whisperlivekit.whisper.tokenizer import get_tokenizer
    tokenizer = get_tokenizer(multilingual=True, language=language, num_languages=100)
    source = tokenizer.encode(text)
    committed, words, _ = _committed_split(tokenizer, source, False, True)
    assert committed == source and "".join(words) == text


def test_cpu_ct2_encoder_feature_bridge_and_decoder_import():
    # Structural smoke only: no model checkpoint or CUDA device is loaded.
    import numpy as np
    import ctranslate2
    from engine.asr_alignatt import _decoder_class
    from engine._vendor.whisperlivekit.simul_whisper.simul_whisper import _encoder_features_to_tensor
    features = np.arange(24, dtype=np.float32).reshape(1, 3, 8)
    storage = ctranslate2.StorageView.from_array(features)
    actual = _encoder_features_to_tensor(storage, "cpu")
    np.testing.assert_array_equal(actual.numpy(), features)
    assert _decoder_class().__name__ == "CommittedAlignAtt"


def test_real_whisper_layer_norm_stays_float32_with_half_decoder_math():
    import torch
    from engine.asr_alignatt import _decoder_precision
    from engine._vendor.whisperlivekit.whisper.model import LayerNorm, Linear
    model = torch.nn.Sequential(LayerNorm(8), Linear(8, 4))
    _decoder_precision(model)
    assert model[0].weight.dtype == torch.float32
    assert model[0].bias.dtype == torch.float32
    assert model[1].weight.dtype == torch.float16
    assert not model.training
    samples = torch.linspace(-1, 1, 16).reshape(2, 8).half()
    output = model(samples)
    assert output.dtype == torch.float16 and output.shape == (2, 4)
    assert torch.isfinite(output).all()


def token(text, start=0.1, end=0.4):
    return SimpleNamespace(text=text, start=start, end=end)


class Decoder:
    def __init__(self, responses=(), language="zh"):
        self.responses = list(responses)
        self.state = SimpleNamespace(detected_language=language)
        self.audio = []
        self.calls = []
        self.resets = 0
        self.global_time_offset = 0

    def insert_audio(self, audio):
        self.audio.append(audio)

    def infer(self, is_last=False):
        self.calls.append(is_last)
        value = self.responses.pop(0) if self.responses else []
        if isinstance(value, Exception):
            raise value
        return value

    def refresh_segment(self, complete=False):
        assert complete
        self.resets += 1


def test_pcm_is_accumulated_until_one_second_then_only_new_audio_inserted():
    decoder = Decoder([[token("第一句")], [token("第二句", 1.1, 1.8)]])
    stream = AlignAttStream(decoder, "zh")
    assert stream.feed(bytes(16000)) == ([], "zh")
    assert decoder.calls == []
    words, language = stream.feed(bytes(16000))
    assert language == "zh" and words[0].text == "第一句"
    assert len(decoder.audio[0]) == 16000
    assert stream.feed(bytes(32000))[0][0].text == "第二句"
    assert len(decoder.audio[1]) == 16000


def test_auto_waits_two_seconds_before_first_commit_then_uses_detected_language():
    decoder = Decoder([[token("Hello")]], "en")
    stream = AlignAttStream(decoder, "auto")
    assert stream.feed(bytes(32000)) == ([], "auto")
    assert decoder.calls == []
    assert stream.feed(bytes(32000))[1] == "en"
    assert len(decoder.audio[0]) == 32000
    stream.feed(bytes(32000))
    assert len(decoder.calls) == 2


def test_final_short_utterance_bypasses_minimum_and_resets_absolute_time():
    decoder = Decoder([[token("はい", 0.02, 0.25)], [token("はい", 0.31, 0.39)]], "ja")
    stream = AlignAttStream(decoder, "auto")
    words, language = stream.feed(bytes(6400), final=True)
    assert words[0].text == "はい" and words[0].end == 0.2
    assert language == "ja" and decoder.calls == [True]
    assert len(decoder.audio[0]) == 6400  # .2 s audio plus .2 s tail
    assert decoder.global_time_offset == 0.2
    words, _ = stream.feed(bytes(6400), final=True)
    assert words[0].text == "はい"  # true repetition after a boundary survives
    assert decoder.global_time_offset == 0.4
    assert stream.feed(b"", final=True) == ([], "ja")
    assert decoder.calls == [True, True]


def test_final_without_new_audio_flushes_once_with_padding():
    decoder = Decoder([[], [token("last", 0.85, 1.1)]], "en")
    stream = AlignAttStream(decoder, "en")
    stream.feed(bytes(32000))
    words, _ = stream.feed(b"", final=True)
    assert words[0].text == "last" and words[0].end == 1.0
    assert len(decoder.audio[-1]) == 3200
    stream.feed(b"", final=True)
    assert decoder.calls == [False, True]


def test_recognition_exception_is_not_converted_to_empty_success():
    decoder = Decoder([RuntimeError("decoder failed")])
    stream = AlignAttStream(decoder, "zh")
    with pytest.raises(RuntimeError, match="decoder failed"):
        stream.feed(bytes(32000))
    assert stream.closed and decoder.resets == 1
    with pytest.raises(RuntimeError, match="closed"):
        stream.feed(bytes(32000))


@pytest.mark.parametrize("start,end", [(float("nan"), 1), (0, float("inf")), (2, 1)])
def test_invalid_timestamps_fail_explicitly(start, end):
    stream = AlignAttStream(Decoder([[token("text", start, end)]]), "zh")
    with pytest.raises(RuntimeError, match="timestamps"):
        stream.feed(bytes(32000))


def test_close_discards_pending_audio_and_is_idempotent():
    decoder = Decoder()
    stream = AlignAttStream(decoder, "en")
    stream.feed(bytes(1000))
    stream.close()
    stream.close()
    assert not stream._pending and decoder.resets == 1
    assert decoder.calls == []


@pytest.mark.parametrize("bad", [b"1", "not bytes", bytes(640002)], ids=["odd-bytes", "not-bytes", "oversized"])
def test_malformed_or_nonincremental_input_is_rejected(bad):
    stream = AlignAttStream(Decoder(), "en")
    with pytest.raises(ValueError):
        stream.feed(bad)


def test_in_memory_decoder_borrows_encoder_without_loading_or_destroying_it():
    encoder = SimpleNamespace(encode=lambda features: features)
    decoder = object()
    backend = AlignAttASR(encoder=encoder, decoder_model=decoder)
    assert backend.model is decoder and backend.encoder is encoder
    backend.close()
    backend.close()
    assert callable(encoder.encode)
    with pytest.raises(RuntimeError, match="closed"):
        backend.create_stream("en")


def test_memory_weights_need_no_checkpoint_path(monkeypatch):
    import engine.asr_alignatt as module
    loaded = []
    def build(dims, state, **kwargs):
        loaded.append((dims, state, kwargs))
        return object()
    monkeypatch.setattr(module, "build_decoder", build)
    encoder = SimpleNamespace(encode=lambda x: x)
    backend = AlignAttASR(encoder=encoder, dimensions={"test": 1}, decoder_state_dict={"test": 2})
    assert loaded == [({"test": 1}, {"test": 2}, {"model_name": "large-v3-turbo"})]
    backend.close()


class FakeBackend:
    def __init__(self, checkpoint, encoder):
        self.checkpoint, self.encoder = checkpoint, encoder
        self.closed = False

    def create_stream(self, language):
        return AlignAttStream(Decoder([[token("confirmed")]], language), language)

    def close(self):
        self.closed = True


@pytest.fixture
def prepared_runtime(tmp_path, monkeypatch):
    from engine.runtime import Runtime
    import engine.asr_alignatt as adapter
    import engine.asr_qwen as qwen
    path = tmp_path / "models/asr/turbo"
    path.mkdir(parents=True)
    (path / "alignatt-decoder.pt").write_bytes(b"mock weights")
    runtime = Runtime(tmp_path)
    runtime.asr = SimpleNamespace(encode=lambda x: x)
    # This fixture intentionally has no real feature/decoder model. Warmup
    # lifecycle and disposable-stream inference are tested separately.
    monkeypatch.setattr(runtime, "_warmup_asr", lambda profile: None)
    runtime.asr_path, runtime.asr_backend = path, "whisper"
    monkeypatch.setattr(adapter, "AlignAttASR", FakeBackend)
    monkeypatch.setattr(qwen, "clear_cuda_cache", lambda: None)
    return runtime, path


def test_runtime_reuses_ct2_and_alignatt_and_unloads_only_decoder(prepared_runtime):
    runtime, path = prepared_runtime
    encoder = runtime.asr
    runtime._load_alignatt(path)
    backend = runtime.asr_alignatt
    assert backend.encoder is encoder
    runtime._load_alignatt(path)
    assert runtime.asr_alignatt is backend
    runtime._unload_alignatt()
    assert backend.closed and runtime.asr_alignatt is None
    assert runtime.asr is encoder and runtime.asr_backend == "whisper"


def test_runtime_final_flush_and_explicit_stream_close(prepared_runtime):
    runtime, path = prepared_runtime
    runtime._load_alignatt(path)
    runtime.asr_profile = "alignatt"
    stream = runtime.create_alignatt_stream("en")
    words, language = runtime.transcribe_alignatt(stream, bytes(6400), final=True)
    assert language == "en" and words[0].text == "confirmed"
    runtime.close_alignatt_stream(stream)
    assert stream.closed


def test_model_unload_also_closes_decoder(prepared_runtime):
    runtime, path = prepared_runtime
    runtime._load_alignatt(path)
    backend = runtime.asr_alignatt
    runtime._unload_asr()
    assert backend.closed
    assert runtime.asr is None and runtime.asr_alignatt is None
    assert runtime.asr_alignatt_path is None


def test_checkpoint_preference_and_missing_file(prepared_runtime):
    from engine.settings import EngineError
    runtime, path = prepared_runtime
    full = path / "large-v3-turbo.pt"
    full.write_bytes(b"mock full weights")
    assert runtime._alignatt_checkpoint(path).name == "alignatt-decoder.pt"
    (path / "alignatt-decoder.pt").unlink()
    assert runtime._alignatt_checkpoint(path) == full
    full.unlink()
    with pytest.raises(EngineError) as error:
        runtime._alignatt_checkpoint(path)
    assert error.value.code == "alignatt_model_missing"


@pytest.mark.asyncio
async def test_prepare_profile_change_keeps_encoder_then_removes_decoder(prepared_runtime, monkeypatch):
    import engine.runtime as module
    runtime, path = prepared_runtime
    encoder = runtime.asr
    monkeypatch.setattr(module, "confined_model", lambda root, value, category: path if category == "asr" else root / "mock.gguf")
    monkeypatch.setattr(module, "validate_gguf", lambda path: None)
    monkeypatch.setattr(module, "asr_backend", lambda path: "whisper")
    async def start(path):
        pass
    monkeypatch.setattr(runtime, "_start_llama", start)
    settings = {"mode": "local", "asr_model": "unused", "translation_model": "unused", "asr_profile": "alignatt"}
    await runtime.prepare(settings)
    backend = runtime.asr_alignatt
    assert runtime.asr is encoder and runtime.asr_profile == "alignatt"
    await runtime.prepare({**settings, "asr_profile": "stable"})
    assert backend.closed and runtime.asr_alignatt is None
    assert runtime.asr is encoder and runtime.asr_profile == "stable"


@pytest.mark.asyncio
async def test_invalid_alignatt_selection_does_not_unload_existing_model(prepared_runtime, monkeypatch):
    from engine.settings import EngineError
    import engine.runtime as module
    runtime, path = prepared_runtime
    encoder = runtime.asr
    monkeypatch.setattr(module, "confined_model", lambda root, value, category: path)
    monkeypatch.setattr(module, "validate_gguf", lambda path: None)
    monkeypatch.setattr(module, "asr_backend", lambda path: "qwen3_asr")
    with pytest.raises(EngineError) as error:
        await runtime.prepare({"mode": "local", "asr_model": "unused", "translation_model": "unused", "asr_profile": "alignatt"})
    assert error.value.code == "asr_profile_unsupported"
    assert runtime.asr is encoder
