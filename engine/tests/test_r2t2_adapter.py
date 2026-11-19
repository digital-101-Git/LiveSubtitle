"""Streaming state and UTF-8 boundaries without loading GPU weights."""
from types import SimpleNamespace

import pytest

from engine.asr_r2t2 import R2T2ASR
from engine.settings import EngineError


class ByteTokenizer:
    def encode(self, text, **kwargs):
        return list(text.encode("utf-8"))

    def decode(self, ids, **kwargs):
        return bytes(ids).decode("utf-8", errors="replace")


def adapter(monkeypatch, responses):
    obj = R2T2ASR.__new__(R2T2ASR)
    obj.processor = SimpleNamespace(tokenizer=ByteTokenizer())
    obj._language_codes = {"chinese": "zh"}
    received = []
    replies = iter(responses)

    def decode(samples, language, prefix, limit):
        received.append((samples.size, prefix))
        return prefix + next(replies)

    monkeypatch.setattr(obj, "_decode", decode)
    return obj, received


def test_stream_appends_audio_holds_partial_character_and_flushes_empty_delta(monkeypatch):
    model, seen = adapter(monkeypatch, ["今天", "天晴", "晴了。"])
    state = model.create_stream("zh")
    assert model.step(state, bytes(10240)) == ("今天", "今")
    assert model.step(state, bytes(10240)) == ("今天晴", "今天")
    assert model.step(state, b"", final=True) == ("今天晴了。", "今天晴了。")
    assert seen == [(5120, ""), (10240, "今"), (10240, "今天")]
    assert state.closed and not state.pcm
    with pytest.raises(EngineError):
        model.step(state, bytes(1024))


def test_auto_language_header_is_never_a_caption(monkeypatch):
    model, seen = adapter(monkeypatch, ["language Chin", "language None<asr_text>",
                                        "language Chinese<asr_text>今天"])
    state = model.create_stream("auto")
    assert model.step(state, bytes(5120)) == ("", "")
    assert model.step(state, bytes(5120)) == ("", "")
    assert model.step(state, bytes(5120), final=True) == ("今天", "今天")
    assert all(prefix == "" for _, prefix in seen)
    assert state.language == "zh"


def test_short_chunk_and_malformed_pcm_do_not_start_inference(monkeypatch):
    model, seen = adapter(monkeypatch, [])
    state = model.create_stream("en")
    assert model.step(state, bytes(100)) == ("", "")
    with pytest.raises(EngineError):
        model.step(state, b"x")
    state.close()
    assert not seen and not state.pcm


def test_unknown_language_on_real_speech_keeps_prefix_without_guessing(monkeypatch):
    model, seen = adapter(monkeypatch, ["language None<asr_text>今天", "天です"])
    state = model.create_stream("auto")
    assert model.step(state, bytes(5120)) == ("今天", "今")
    assert state.language == "auto"
    assert model.step(state, bytes(5120), final=True) == ("今天です", "今天です")
    assert state.language == "auto"
    assert seen[-1][1] == "language None<asr_text>今"
