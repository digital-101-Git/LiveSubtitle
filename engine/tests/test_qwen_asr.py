"""Local Qwen adapter contract tests; never import GPU runtimes or download weights."""
import json
import struct
import sys
import weakref
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from engine.asr_qwen import MAX_NEW_TOKENS, QwenASR
from engine.models import ASR_FILES, QWEN_ASR_FILES, asr_backend, inventory
from engine.runtime import Runtime
from engine.settings import EngineError


def safetensors(path, tensor="weight"):
    header = json.dumps({tensor: {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    header += b" " * (-len(header) % 8)
    path.write_bytes(struct.pack("<Q", len(header)) + header + struct.pack("<f", 1.0))


def qwen_files(path):
    path.mkdir(parents=True)
    for name in QWEN_ASR_FILES:
        (path / name).write_text("{}", encoding="utf-8")
    (path / "config.json").write_text(json.dumps({"model_type": "qwen3_asr",
        "architectures": ["Qwen3ASRForConditionalGeneration"]}), encoding="utf-8")
    safetensors(path / "model.safetensors")
    return path


def whisper_files(path):
    path.mkdir(parents=True)
    for name in ASR_FILES:
        (path / name).write_text("{}", encoding="utf-8")
    return path


def test_inventory_identifies_whisper_and_complete_native_qwen(tmp_path):
    qwen = qwen_files(tmp_path / "models/asr/qwen")
    whisper = whisper_files(tmp_path / "models/asr/whisper")
    assert asr_backend(qwen) == "qwen3_asr"
    assert asr_backend(whisper) == "whisper"
    models = inventory(tmp_path)["asr"]
    assert [(entry["path"], entry["backend"]) for entry in models] == [
        ("models/asr/qwen", "qwen3_asr"), ("models/asr/whisper", "whisper")]


@pytest.mark.parametrize("broken", ["weights", "processor", "architecture"])
def test_inventory_excludes_incomplete_or_wrong_qwen(tmp_path, broken):
    path = qwen_files(tmp_path / "models/asr/qwen")
    if broken == "weights":
        weight = path / "model.safetensors"
        weight.write_bytes(weight.read_bytes()[:-1])
    elif broken == "processor":
        (path / "processor_config.json").unlink()
    else:
        (path / "config.json").write_text(json.dumps({"model_type": "qwen3_asr",
            "architectures": ["Qwen3ASRForTokenClassification"]}))
    assert inventory(tmp_path)["asr"] == []
    with pytest.raises(EngineError, match="음성 인식 모델") as caught:
        asr_backend(path)
    assert caught.value.code == "asr_incomplete"


def test_sharded_safetensors_requires_all_indexed_tensors(tmp_path):
    path = qwen_files(tmp_path / "models/asr/qwen")
    (path / "model.safetensors").unlink()
    index = {"weight_map": {"first": "one.safetensors", "second": "two.safetensors"}}
    (path / "model.safetensors.index.json").write_text(json.dumps(index))
    safetensors(path / "one.safetensors", "first")
    assert inventory(tmp_path)["asr"] == []
    safetensors(path / "two.safetensors", "second")
    assert asr_backend(path) == "qwen3_asr"
    safetensors(path / "two.safetensors", "wrong")
    assert inventory(tmp_path)["asr"] == []


@pytest.mark.parametrize("filename", ["../outside.safetensors", "..\\outside.safetensors", "C:outside.safetensors"])
def test_weight_index_cannot_escape_model_directory(tmp_path, filename):
    path = qwen_files(tmp_path / "models/asr/qwen")
    (path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"x": filename}}))
    assert inventory(tmp_path)["asr"] == []


@pytest.fixture
def providers(monkeypatch):
    # These provider mocks exercise transcription/loading, not preparation
    # kernels. Dedicated test_asr_warmup covers the actual warmup contract.
    monkeypatch.setattr(QwenASR, "warmup", lambda self: None)
    monkeypatch.setattr("engine.runtime.warmup_whisper", lambda model: None)
    state = SimpleNamespace(loads=[], device_moves=[], input_moves=[], requests=[], decoded=[],
                            inference_depth=0, cache_clears=0, fail_load=False, cuda=True,
                            has_speech=True, vad_inputs=[],
                            generated=[101, 102, 99], parsed=[{"language": "Japanese", "transcription": " 原文 "}])

    @contextmanager
    def inference():
        state.inference_depth += 1
        try:
            yield
        finally:
            state.inference_depth -= 1

    def empty_cache():
        state.cache_clears += 1

    class Batch(dict):
        def to(self, device, dtype):
            state.input_moves.append((device, dtype))
            return self

    class Processor:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            state.loads.append(("processor", path, kwargs))
            return cls()

        def apply_transcription_request(self, **kwargs):
            assert state.inference_depth == 1
            state.requests.append(kwargs)
            return Batch(input_ids=np.array([[10, 11, 12]]))

        def decode(self, generated, **kwargs):
            state.decoded.append((generated.tolist(), kwargs))
            return state.parsed

    class Model:
        device = "cuda"
        dtype = "bf16"
        generation_config = SimpleNamespace(eos_token_id=99)

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            state.loads.append(("model", path, kwargs))
            if state.fail_load:
                raise RuntimeError("simulated allocation failure")
            return cls()

        def to(self, device):
            state.device_moves.append(device)
            return self

        def eval(self):
            state.evaluating = True
            return self

        def generate(self, **kwargs):
            assert state.inference_depth == 1
            assert kwargs["do_sample"] is False and kwargs["max_new_tokens"] == MAX_NEW_TOKENS
            return np.array([[10, 11, 12] + state.generated])

    torch = SimpleNamespace(bfloat16="bf16", inference_mode=inference,
        cuda=SimpleNamespace(is_available=lambda: state.cuda, is_initialized=lambda: True, empty_cache=empty_cache))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoProcessor=Processor, AutoModelForMultimodalLM=Model))
    def vad(samples, **kwargs):
        assert kwargs == {"sampling_rate": 16000}
        state.vad_inputs.append(samples)
        # Returned boundaries intentionally exclude samples: the adapter must
        # forward the original waveform, not slice it to these timestamps.
        return [{"start": 1, "end": 2}] if state.has_speech else []

    monkeypatch.setitem(sys.modules, "faster_whisper.vad", SimpleNamespace(get_speech_timestamps=vad))
    return state


@pytest.mark.parametrize("selected,forwarded,returned", [
    ("en", "en", "en"), ("zh", "zh", "zh"), ("ja", "ja", "ja"), ("ko", "ko", "ko"),
    ("auto", None, "ja"), (None, None, "ja"), (" ", None, "ja"),
])
def test_qwen_forced_and_detected_language_and_pcm_contract(tmp_path, providers, selected, forwarded, returned):
    adapter = QwenASR(tmp_path)
    samples = np.array([-.5, 0, .5], dtype=np.float32)
    assert adapter.transcribe(samples, selected) == ("原文", returned)
    request = providers.requests[-1]
    assert request["audio"] is samples
    assert providers.vad_inputs[-1] is samples
    assert request["language"] == forwarded
    assert request["processor_kwargs"] == {"audio_kwargs": {"sampling_rate": 16000}}
    assert "audio_kwargs" not in request
    assert "prompt" not in request  # Empty/default hints preserve the former request.
    assert providers.decoded == [([[101, 102, 99]], {"return_format": "parsed"})]
    assert providers.input_moves == [("cuda", "bf16")]
    assert providers.device_moves == ["cuda"] and providers.evaluating
    assert all(call[2]["local_files_only"] and call[2]["trust_remote_code"] is False for call in providers.loads)
    assert providers.loads[1][2]["use_safetensors"] is True
    adapter.close()
    assert adapter.model is None and adapter.processor is None and providers.cache_clears == 1


@pytest.mark.parametrize("selected", ["auto", "zh"])
def test_qwen_no_speech_gate_skips_processor_and_generation(tmp_path, providers, selected):
    adapter = QwenASR(tmp_path)
    providers.has_speech = False
    for samples in (np.zeros(16000, dtype=np.float32),
                    np.random.default_rng(7).normal(0, .02, 16000).astype(np.float32)):
        assert adapter.transcribe(samples, selected) == ("", selected)
    assert len(providers.vad_inputs) == 2 and providers.requests == [] and providers.decoded == []


def test_qwen_truncation_does_not_emit_incomplete_transcript(tmp_path, providers):
    adapter = QwenASR(tmp_path)
    providers.generated = [101] * MAX_NEW_TOKENS
    with pytest.raises(EngineError) as caught:
        adapter.transcribe(np.zeros(1600, dtype=np.float32), "en")
    assert caught.value.code == "asr_truncated" and providers.decoded == []
    providers.generated[-1] = 99
    assert adapter.transcribe(np.zeros(1600, dtype=np.float32), "en") == ("原文", "en")


def test_qwen_missing_cuda_and_load_failure_are_explicit(tmp_path, providers):
    providers.cuda = False
    with pytest.raises(EngineError) as caught:
        QwenASR(tmp_path)
    assert caught.value.code == "asr_gpu_missing" and providers.loads == []
    providers.cuda = True
    providers.fail_load = True
    with pytest.raises(EngineError) as caught:
        QwenASR(tmp_path)
    assert caught.value.code == "asr_load_failed" and providers.cache_clears == 1


def test_runtime_switch_qwen_to_whisper_releases_gpu_before_loading(tmp_path, providers, monkeypatch):
    qwen = qwen_files(tmp_path / "models/asr/qwen")
    whisper = whisper_files(tmp_path / "models/asr/whisper")
    runtime = Runtime(tmp_path)
    runtime._load_asr(qwen)
    assert runtime.asr_backend == "qwen3_asr"
    assert runtime.transcribe(struct.pack("<3h", -16384, 0, 16384), "zh") == ("原文", "zh")
    np.testing.assert_array_equal(providers.requests[-1]["audio"], [-.5, 0, .5])
    assert providers.requests[-1]["audio"].dtype == np.float32
    adapter = runtime.asr

    class Whisper:
        def __init__(self, path, **kwargs):
            assert adapter.model is None and providers.cache_clears == 1
            assert kwargs["local_files_only"] is True and kwargs["compute_type"] == "int8_float16"

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    runtime._load_asr(whisper)
    assert runtime.asr_backend == "whisper" and isinstance(runtime.asr, Whisper)
    runtime._load_asr(qwen)
    runtime._unload_asr()
    assert runtime.asr is None and runtime.asr_path is None and providers.cache_clears == 3


def test_runtime_incomplete_replacement_keeps_current_asr(tmp_path, providers):
    qwen = qwen_files(tmp_path / "models/asr/qwen")
    runtime = Runtime(tmp_path)
    runtime._load_asr(qwen)
    previous = runtime.asr
    with pytest.raises(EngineError):
        runtime._load_asr(tmp_path / "models/asr/missing")
    assert runtime.asr is previous and providers.cache_clears == 0
    runtime._unload_asr()


def test_runtime_clears_torch_cache_again_after_failed_qwen_load(tmp_path, providers, monkeypatch):
    qwen = qwen_files(tmp_path / "models/asr/qwen")
    whisper = whisper_files(tmp_path / "models/asr/whisper")
    runtime = Runtime(tmp_path)
    providers.fail_load = True
    with pytest.raises(EngineError):
        runtime._load_asr(qwen)
    assert runtime.asr is None and providers.cache_clears == 1

    class Whisper:
        def __init__(self, *args, **kwargs):
            assert providers.cache_clears == 2

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    runtime._load_asr(whisper)
    assert runtime.asr_backend == "whisper"


def test_failed_device_transfer_drops_traceback_allocations_before_cache_clear(tmp_path, providers, monkeypatch):
    allocations = []

    class Allocation:
        pass

    def failed_transfer(self, device):
        allocation = Allocation()
        allocations.append(weakref.ref(allocation))
        raise RuntimeError("simulated device-transfer OOM")

    def clear():
        # The transfer frame's local allocation must already be collectible.
        assert allocations and allocations[0]() is None
        providers.cache_clears += 1

    monkeypatch.setattr(sys.modules["transformers"].AutoModelForMultimodalLM, "to", failed_transfer)
    monkeypatch.setattr(sys.modules["torch"].cuda, "empty_cache", clear)
    with pytest.raises(EngineError) as caught:
        QwenASR(tmp_path)
    assert caught.value.code == "asr_load_failed" and providers.cache_clears == 1
    assert caught.value.__cause__.__traceback__ is None


@pytest.mark.asyncio
async def test_prepare_unloads_replaced_asr_before_loading_new_llama(tmp_path, providers, monkeypatch):
    qwen = qwen_files(tmp_path / "models/asr/qwen")
    whisper = whisper_files(tmp_path / "models/asr/whisper")
    translation = tmp_path / "models/translation/new.gguf"
    translation.parent.mkdir(parents=True)
    translation.write_bytes(b"GGUFmodel")
    runtime = Runtime(tmp_path)
    runtime._load_asr(qwen)
    events = []

    async def start_llama(path):
        assert path == translation and runtime.asr is None
        assert providers.cache_clears >= 1
        events.append("llama")

    class Whisper:
        def __init__(self, *args, **kwargs):
            assert events == ["llama"]
            events.append("whisper")

    monkeypatch.setattr(runtime, "_start_llama", start_llama)
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    await runtime.prepare({"mode": "local", "asr_model": "models/asr/whisper",
                           "translation_model": "models/translation/new.gguf"})
    assert events == ["llama", "whisper"] and runtime.asr_path == whisper


@pytest.mark.asyncio
async def test_prepare_validates_replacement_before_unloading_current_models(tmp_path, providers, monkeypatch):
    qwen = qwen_files(tmp_path / "models/asr/qwen")
    translation = tmp_path / "models/translation/new.gguf"
    translation.parent.mkdir(parents=True)
    translation.write_bytes(b"GGUFmodel")
    runtime = Runtime(tmp_path)
    runtime._load_asr(qwen)
    previous = runtime.asr

    async def unexpected_start(path):
        pytest.fail("Invalid ASR must not replace the translation model")

    monkeypatch.setattr(runtime, "_start_llama", unexpected_start)
    with pytest.raises(EngineError):
        await runtime.prepare({"mode": "local", "asr_model": "models/asr/missing",
                               "translation_model": "models/translation/new.gguf"})
    assert runtime.asr is previous and providers.cache_clears == 0


@pytest.mark.asyncio
async def test_prepare_same_models_reuses_asr_and_llama_process(tmp_path, providers, monkeypatch):
    qwen = qwen_files(tmp_path / "models/asr/qwen")
    translation = tmp_path / "models/translation/same.gguf"
    translation.parent.mkdir(parents=True)
    translation.write_bytes(b"GGUFmodel")
    runtime = Runtime(tmp_path)
    runtime._load_asr(qwen)
    previous = runtime.asr
    process = SimpleNamespace(poll=lambda: None)
    runtime.process, runtime.translation_path = process, translation
    await runtime.prepare({"mode": "local", "asr_model": "models/asr/qwen",
                           "translation_model": "models/translation/same.gguf"})
    assert runtime.asr is previous and runtime.process is process
    assert providers.cache_clears == 0 and len(providers.loads) == 2


def test_qwen_optional_hint_prompt_preserves_audio_language_and_inference_contract(tmp_path, providers):
    adapter = QwenASR(tmp_path)
    samples = np.array([-.5, 0, .5], dtype=np.float32)
    assert adapter.transcribe(samples, "zh", prompt="Vocabulary: 東京, Hunyuan") == ("原文", "zh")
    request = providers.requests[-1]
    assert request["prompt"] == "Vocabulary: 東京, Hunyuan"
    assert request["audio"] is samples and providers.vad_inputs[-1] is samples
    assert request["language"] == "zh"
    adapter.transcribe(samples, "auto", prompt="")
    assert "prompt" not in providers.requests[-1] and providers.requests[-1]["language"] is None


def loaded_hint_runtime(tmp_path, providers):
    qwen = qwen_files(tmp_path / "models/asr/qwen")
    translation = tmp_path / "models/translation/same.gguf"
    translation.parent.mkdir(parents=True)
    translation.write_bytes(b"GGUFmodel")
    runtime = Runtime(tmp_path)
    runtime._load_asr(qwen)
    runtime.process = SimpleNamespace(poll=lambda: None)
    runtime.translation_path = translation
    return runtime, {"mode": "local", "asr_model": "models/asr/qwen",
                     "translation_model": "models/translation/same.gguf"}


@pytest.mark.asyncio
async def test_prepare_updates_and_clears_qwen_hints_without_model_or_process_reload(tmp_path, providers):
    runtime, settings = loaded_hint_runtime(tmp_path, providers)
    adapter, process = runtime.asr, runtime.process
    pcm = struct.pack("<3h", -16384, 0, 16384)
    await runtime.prepare(settings)
    runtime.transcribe(pcm, "zh")
    assert runtime.asr_hints == () and "prompt" not in providers.requests[-1]

    supplied = [" 東京 ", "Hunyuan", "東京"]
    await runtime.prepare({**settings, "asr_hints": supplied})
    supplied.append("not prepared")
    runtime.transcribe(pcm, "zh")
    assert runtime.asr_hints == ("東京", "Hunyuan")
    assert providers.requests[-1]["prompt"] == "Vocabulary: 東京, Hunyuan"

    await runtime.prepare({**settings, "asr_hints": ["Next"]})
    runtime.transcribe(pcm, "auto")
    assert providers.requests[-1]["prompt"] == "Vocabulary: Next"
    assert providers.requests[-1]["language"] is None
    await runtime.prepare({**settings, "asr_hints": []})
    runtime.transcribe(pcm, "zh")
    assert "prompt" not in providers.requests[-1] and runtime.asr_hint_prompt == ""
    assert runtime.asr is adapter and runtime.process is process
    assert providers.cache_clears == 0 and len(providers.loads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "name", ["<|im_start|>"], ["x" * 49]])
async def test_invalid_runtime_hints_are_rejected_before_model_changes_or_provider_calls(tmp_path, providers, bad):
    runtime, settings = loaded_hint_runtime(tmp_path, providers)
    await runtime.prepare({**settings, "asr_hints": ["Existing"]})
    adapter, process = runtime.asr, runtime.process
    with pytest.raises(EngineError) as error:
        await runtime.prepare({**settings, "asr_hints": bad})
    assert error.value.code == "invalid_asr_hints" and error.value.status == 400
    assert runtime.asr is adapter and runtime.process is process
    assert runtime.asr_hints == ("Existing",) and runtime.asr_hint_prompt == "Vocabulary: Existing"
    assert len(providers.loads) == 2 and not providers.requests and providers.cache_clears == 0


def test_runtime_never_forwards_qwen_hint_prompt_to_whisper(tmp_path):
    runtime = Runtime(tmp_path)
    observed = []

    def transcribe(samples, **kwargs):
        observed.append(kwargs)
        return iter([SimpleNamespace(text="Hello", no_speech_prob=0, avg_logprob=0,
                                     compression_ratio=1)]), SimpleNamespace(language="en")

    runtime.asr = SimpleNamespace(transcribe=transcribe)
    runtime.asr_backend = "whisper"
    runtime.asr_hints = ("Tokyo",)
    runtime.asr_hint_prompt = "Vocabulary: Tokyo"
    assert runtime.transcribe(struct.pack("<3h", -1000, 0, 1000), "auto") == ("Hello", "en")
    assert "prompt" not in observed[0] and "initial_prompt" not in observed[0]
