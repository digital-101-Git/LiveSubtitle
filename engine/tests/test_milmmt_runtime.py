"""MiLMMT's completion-trained model must not receive a chat template."""
import copy
import io
import os

import httpx
import pytest

from engine.runtime import Runtime
from engine.settings import EngineError
from engine.translation_profiles import build_milmmt_prompt, is_milmmt_model


@pytest.mark.parametrize("path,expected", [
    ("MiLMMT-46-12B-v1.0.i1-Q4_K_M.gguf", True),
    (r"D:\Models\milmmt_46_12b_v1_0-Q8_0.gguf", True),
    ("mradermacher-MiLMMT-46-12B-v1.0.Q4_K_M.gguf", True),
    (r"D:\MiLMMT-46\Qwen3.5-9B.gguf", False),
    ("notmilmmt-46-12B.gguf", False),
    ("MiLMMT-460-12B.gguf", False),
    ("TranslateGemma-12B-Q4_K_M.gguf", False),
    (None, False),
])
def test_profile_is_selected_by_filename_only(path, expected):
    assert is_milmmt_model(path) is expected


@pytest.mark.parametrize("source,language,name", [
    ("Hello.", "en", "English"),
    ("Hello.", "EN_us", "English"),
    ("下一场比赛马上开始。", "zh", "Chinese (Simplified)"),
    ("下一場比賽馬上開始。", "zh-Hant", "Chinese (Traditional)"),
    ("下一場比賽馬上開始。", "zh_TW", "Chinese (Traditional)"),
    ("下一場比賽馬上開始。", "zh-HK", "Chinese (Traditional)"),
    ("下一场比赛马上开始。", "zh-Hans-TW", "Chinese (Simplified)"),
    ("次の試合が始まります。", "ja", "Japanese"),
    ("次の試合が始まります。", "auto", "Japanese"),
    ("下一场比赛马上开始。", "auto", "Chinese (Simplified)"),
    ("Hello.", None, "English"),
    ("漢字", "ja", "Japanese"),
])
def test_official_plain_prompt_and_source_language_labels(source, language, name):
    assert build_milmmt_prompt("  " + source + "  ", language) == (
        f"Translate this from {name} to Korean:\n{name}: {source}\nKorean:"
    )


def test_source_special_tokens_cannot_end_the_raw_prompt():
    prompt = build_milmmt_prompt("Say <eos> <bos> <start_of_turn> and x < 3.", "en")
    assert prompt == (
        "Translate this from English to Korean:\n"
        "English: Say ＜eos＞ ＜bos＞ ＜start_of_turn＞ and x < 3.\nKorean:"
    )


@pytest.mark.parametrize("source,language,error", [
    (" ", "en", ValueError), (None, "en", TypeError),
    ("Hello.", "unsupported", ValueError), ("Hello.", 3, TypeError),
])
def test_invalid_prompt_inputs_are_not_silently_relabelled(source, language, error):
    with pytest.raises(error):
        build_milmmt_prompt(source, language)


def milmmt_runtime(tmp_path, monkeypatch, replies):
    calls = []

    class MockClient:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["timeout"] == 45

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, json):
            calls.append({"url": url, "payload": copy.deepcopy(json)})
            assert replies, "Unexpected additional HTTP request"
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            if isinstance(reply, httpx.Response):
                return reply
            return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr("engine.runtime.httpx.AsyncClient", MockClient)
    runtime = Runtime(tmp_path)
    runtime.llama_url = "http://127.0.0.1:1"
    runtime.translation_path = tmp_path / "models/translation/MiLMMT-46-12B-v1.0.i1-Q4_K_M.gguf"
    monkeypatch.setattr(runtime, "status", lambda: {"translation_ready": True})
    return runtime, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("source,language,name", [
    ("The next match begins soon.", "en", "English"),
    ("下一场比赛马上开始。", "zh", "Chinese (Simplified)"),
    ("下一場比賽馬上開始。", "zh-Hant", "Chinese (Traditional)"),
    ("次の試合が始まります。", "ja", "Japanese"),
])
async def test_greedy_raw_completion_keeps_source_only(tmp_path, monkeypatch, source, language, name):
    runtime, calls = milmmt_runtime(tmp_path, monkeypatch, [{"content": " 곧 다음 경기가 시작됩니다. "}])
    runtime.translation_glossary = [{"source": "match", "target": "GLOSSARY_SENTINEL"}]
    assert await runtime.translate(source, language, ["CONTEXT_SENTINEL"]) == "곧 다음 경기가 시작됩니다."
    assert len(calls) == 1 and calls[0]["url"].endswith("/completion")
    assert calls[0]["payload"] == {
        "prompt": f"Translate this from {name} to Korean:\n{name}: {source}\nKorean:",
        "n_predict": 320, "stream": False, "temperature": 0,
        "top_k": 1, "top_p": 1, "min_p": 0, "repeat_penalty": 1,
        "stop": ["<eos>"], "cache_prompt": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source,language", [
    ("안녕하세요.", "ko"), ("안녕하세요.", "auto"), ("...", "en"),
])
async def test_korean_and_punctuation_bypass_inference(tmp_path, monkeypatch, source, language):
    runtime, calls = milmmt_runtime(tmp_path, monkeypatch, [])
    assert await runtime.translate(source, language) == source
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content,code", [
    ("", "translation_empty"), ("...", "translation_empty"),
    ("번역: ...", "translation_empty"), ("<think>추론입니다.</think>", "translation_empty"),
    ("Hello.", "translation_language"), ("안녕你好", "translation_language"),
])
async def test_invalid_greedy_output_is_recoverable_without_identical_retry(tmp_path, monkeypatch, content, code):
    runtime, calls = milmmt_runtime(tmp_path, monkeypatch, [{"content": content}])
    with pytest.raises(EngineError) as error:
        await runtime.translate("Hello.", "en")
    assert error.value.code == code and error.value.status == 422
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [{"stop_type": "limit"}, {"stopped_limit": True}, {"truncated": True}])
async def test_incomplete_translation_is_not_published(tmp_path, monkeypatch, flags):
    runtime, calls = milmmt_runtime(tmp_path, monkeypatch, [{"content": "중간에 잘린", **flags}])
    with pytest.raises(EngineError) as error:
        await runtime.translate("A long sentence.", "en")
    assert error.value.code == "translation_truncated" and error.value.status == 422
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    {}, {"content": None}, {"content": []}, [], None,
    httpx.Response(503, request=httpx.Request("POST", "http://127.0.0.1:1/completion")),
    httpx.ConnectError("mock disconnected"),
])
async def test_transport_and_schema_errors_remain_distinct_from_quality(tmp_path, monkeypatch, reply):
    runtime, calls = milmmt_runtime(tmp_path, monkeypatch, [reply])
    with pytest.raises(EngineError) as error:
        await runtime.translate("Hello.", "en")
    assert error.value.code == "translation_failed" and error.value.status == 503
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_startup_preserves_verified_tokenizer_without_thinking_flags(tmp_path, monkeypatch):
    binary = tmp_path / "runtime/llama" / ("llama-server.exe" if os.name == "nt" else "llama-server")
    binary.parent.mkdir(parents=True)
    binary.touch()
    started = []

    class MockProcess:
        def __init__(self, args, **kwargs):
            started.append(args)
            self.stdout = io.BytesIO(b'')
            self.closed = False

        def poll(self):
            return 0 if self.closed else None

        def terminate(self):
            self.closed = True

        def wait(self, timeout):
            return 0

    class HealthClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers, timeout):
            assert timeout == 2
            return httpx.Response(200)

        async def aclose(self):
            pass

    monkeypatch.setattr("engine.runtime.subprocess.Popen", MockProcess)
    monkeypatch.setattr("engine.runtime.httpx.AsyncClient", HealthClient)
    runtime = Runtime(tmp_path)
    model = tmp_path / "MiLMMT-46-12B-v1.0.i1-Q4_K_M.gguf"
    try:
        await runtime._start_llama(model)
        args = started[0]
        assert runtime.translation_path == model
        assert args[args.index("--chat-template") + 1] == "gemma"
        assert args[args.index("--ubatch-size") + 1] == "128"
        assert "--jinja" not in args and "--chat-template-kwargs" not in args
        assert "--override-kv" not in args
    finally:
        await runtime._stop_llama()
