"""Whisper streaming adapter checks without loading a model or GPU runtime."""
from types import SimpleNamespace
import struct

import numpy as np
import pytest

from engine.local_streaming import ASRWord
from engine.runtime import Runtime
from engine.settings import EngineError


def segment(text="Hello.", *, words=None, start=.1, end=.7,
            no_speech_prob=.1, avg_logprob=-.2, compression_ratio=1.1):
    return SimpleNamespace(text=text, words=words, start=start, end=end,
                           no_speech_prob=no_speech_prob, avg_logprob=avg_logprob,
                           compression_ratio=compression_ratio)


class Recognizer:
    def __init__(self, segments=(), language="en"):
        self.segments, self.language = segments, language
        self.calls = []

    def transcribe(self, samples, **options):
        self.calls.append((samples.copy(), options))
        return iter(self.segments), SimpleNamespace(language=self.language)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    # Do not change DLL paths or inspect the installed model directories.
    monkeypatch.setattr("engine.runtime.configure_cuda", lambda root: None)
    result = Runtime(tmp_path)
    result.asr_backend = "whisper"
    return result


def test_streaming_adapter_requests_word_timings_and_preserves_pcm_scaling(runtime):
    runtime.asr = Recognizer([segment(words=[
        SimpleNamespace(start=.1, end=.4, word=" Hello"),
        SimpleNamespace(start=.4, end=.7, word=" world."),
        SimpleNamespace(start=.7, end=.7, word=""),
    ])])
    words, language = runtime.transcribe_stream(struct.pack("<3h", -32768, 0, 32767))
    samples, options = runtime.asr.calls[0]
    assert samples.dtype == np.float32
    np.testing.assert_array_equal(samples, [-1.0, 0.0, 32767 / 32768])
    assert words == [ASRWord(.1, .4, " Hello"), ASRWord(.4, .7, " world.")]
    assert language == "en"
    assert options["word_timestamps"] is True
    assert options["task"] == "transcribe"
    assert options["condition_on_previous_text"] is False
    assert options["vad_filter"] is True
    assert options["vad_parameters"] == {"min_silence_duration_ms": 500}


@pytest.mark.parametrize("selected,detected,passed,returned", [
    ("auto", "ja", None, "ja"),
    ("ja", "en", "ja", "ja"),
    ("zh", "en", "zh", "zh"),
    ("en", "zh", "en", "en"),
])
def test_selected_language_is_authoritative(runtime, selected, detected, passed, returned):
    runtime.asr = Recognizer(language=detected)
    _, language = runtime.transcribe_stream(b"\0\0", selected)
    assert runtime.asr.calls[0][1]["language"] == passed
    assert language == returned


def test_segment_metadata_filters_hallucination_without_discarding_low_confidence_speech(runtime):
    runtime.asr = Recognizer([
        segment("silence hallucination", no_speech_prob=.9, avg_logprob=-2),
        segment("repetition hallucination", compression_ratio=2.5),
        segment("audible despite VAD", no_speech_prob=.9, avg_logprob=-.2),
        segment("uncertain but voiced", no_speech_prob=.1, avg_logprob=-2),
        segment("boundary accepted", no_speech_prob=.6, avg_logprob=-1, compression_ratio=2.4),
        segment(" \t ", words=[]),
    ])
    words, _ = runtime.transcribe_stream(b"\0\0")
    assert [word.text for word in words] == [
        "audible despite VAD", "uncertain but voiced", "boundary accepted"]
    # A backend without word timings still gives the controller a timed segment.
    assert all((word.start, word.end) == (.1, .7) for word in words)


@pytest.mark.parametrize("prompt,expected", [("", None), ("짧은 문맥", "짧은 문맥"),
                                             ("old" * 200 + "최근 문맥", ("old" * 200 + "최근 문맥")[-500:])])
def test_initial_prompt_is_the_latest_500_characters(runtime, prompt, expected):
    runtime.asr = Recognizer()
    runtime.transcribe_stream(b"\0\0", "auto", prompt)
    assert runtime.asr.calls[0][1]["initial_prompt"] == expected


@pytest.mark.parametrize("backend,code", [(None, "asr_not_ready"), ("qwen3_asr", "asr_streaming_unsupported")])
def test_missing_model_and_non_whisper_backend_fail_before_inference(runtime, backend, code):
    recognizer = Recognizer()
    runtime.asr_backend = backend
    runtime.asr = recognizer if backend else None
    with pytest.raises(EngineError) as caught:
        runtime.transcribe_stream(b"\0\0")
    assert (caught.value.code, caught.value.status) == (code, 409)
    assert recognizer.calls == []


def test_asr_lock_covers_lazy_segment_iteration_and_is_released_after_failure(runtime):
    def segments():
        assert runtime.asr_lock.locked()
        yield segment("first")
        assert runtime.asr_lock.locked()
        raise RuntimeError("synthetic decoder failure")

    runtime.asr = Recognizer(segments())
    with pytest.raises(EngineError) as caught:
        runtime.transcribe_stream(b"\0\0")
    assert (caught.value.code, caught.value.status) == ("asr_failed", 503)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert not runtime.asr_lock.locked()

