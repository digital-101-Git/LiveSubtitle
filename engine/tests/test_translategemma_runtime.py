"""TranslateGemma transport contracts; all HTTP responses are local mocks."""
import copy

import httpx
import pytest

from engine.runtime import Runtime, korean_output
from engine.settings import EngineError


def gemma_runtime(tmp_path, monkeypatch, replies, model="TranslateGemma-12B-Q4_K_M.gguf"):
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
    runtime.translation_path = tmp_path / "models" / "translation" / model
    monkeypatch.setattr(runtime, "status", lambda: {"translation_ready": True})
    return runtime, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("source,language", [
    ("The next match begins soon.", "en"),
    ("下一场比赛马上开始。", "zh"),
    ("次の試合が始まります。", "ja"),
])
async def test_translate_gemma_uses_source_only_raw_completion_prompt(tmp_path, monkeypatch, source, language):
    runtime, calls = gemma_runtime(tmp_path, monkeypatch, [{"content": "곧 다음 경기가 시작됩니다."}])
    result = await runtime.translate(source, language, ["PRIVATE_CONTEXT_SENTINEL_DO_NOT_SEND"])
    assert result == "곧 다음 경기가 시작됩니다."
    assert len(calls) == 1 and calls[0]["url"].endswith("/completion")
    payload = calls[0]["payload"]
    assert set(payload) == {"prompt", "n_predict", "stream", "temperature", "stop", "cache_prompt"}
    assert isinstance(payload["prompt"], str) and source in payload["prompt"]
    assert "PRIVATE_CONTEXT_SENTINEL_DO_NOT_SEND" not in payload["prompt"]
    assert '"current_text"' not in payload["prompt"] and '"messages"' not in payload["prompt"]
    assert '"source_language"' not in payload["prompt"] and '"role": "system"' not in payload["prompt"]
    assert payload["n_predict"] == 320 and payload["temperature"] == 0
    assert payload["stream"] is False and payload["cache_prompt"] is True
    assert payload["stop"] == ["<end_of_turn>", "<eos>"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["Still English.", "", " \n\t", "<think>unfinished", "…", "번역: ..."])
async def test_quality_retry_is_bounded_and_reuses_source_only_prompt(tmp_path, monkeypatch, first):
    runtime, calls = gemma_runtime(tmp_path, monkeypatch, [{"content": first}, {"content": "네, 맞습니다."}])
    assert await runtime.translate("Yes, that's right.", "en", ["CONTEXT_SENTINEL"]) == "네, 맞습니다."
    assert len(calls) == 2
    assert [call["payload"]["temperature"] for call in calls] == [0, .2]
    assert calls[0]["payload"]["prompt"] == calls[1]["payload"]["prompt"]
    assert all(call["url"].endswith("/completion") for call in calls)
    assert all("CONTEXT_SENTINEL" not in call["payload"]["prompt"] for call in calls)
    assert all(call["payload"]["n_predict"] == 320 for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ko", "auto"])
async def test_confirmed_korean_source_returns_without_request(tmp_path, monkeypatch, language):
    runtime, calls = gemma_runtime(tmp_path, monkeypatch, [])
    assert await runtime.translate("안녕하세요. 오늘 방송입니다.", language, ["unrelated context"]) == "안녕하세요. 오늘 방송입니다."
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    {}, {"content": None}, {"content": []}, {"content": {"text": "잘못된 구조"}},
    [], None, 5,
    httpx.Response(200, content=b"invalid json", request=httpx.Request("POST", "http://127.0.0.1:1/completion")),
    httpx.Response(503, json={"error": "mock unavailable"}, request=httpx.Request("POST", "http://127.0.0.1:1/completion")),
    httpx.ConnectError("mock disconnected"),
])
async def test_missing_content_bad_schema_and_transport_are_503_failures(tmp_path, monkeypatch, reply):
    runtime, calls = gemma_runtime(tmp_path, monkeypatch, [reply])
    with pytest.raises(EngineError) as error:
        await runtime.translate("Hello.", "en")
    assert error.value.code == "translation_failed" and error.value.status == 503
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [{"stop_type": "limit"}, {"stopped_limit": True}, {"truncated": True}])
async def test_truncation_is_quality_error_without_reclassifying_or_retrying(tmp_path, monkeypatch, flags):
    runtime, calls = gemma_runtime(tmp_path, monkeypatch, [{"content": "잘린 번역입니다", **flags}])
    with pytest.raises(EngineError) as error:
        await runtime.translate("A longer source sentence.", "en")
    assert error.value.code == "translation_truncated" and error.value.status == 422
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outputs,code", [
    (["Hello there.", "Still English."], "translation_language"),
    (["안녕하세요你好", "こんにちは입니다"], "translation_language"),
    (["", " \n"], "translation_empty"),
    (["…", "..."], "translation_empty"),
    (["Foreign text", ""], "translation_empty"),
    (["", "Foreign text"], "translation_language"),
])
async def test_two_invalid_outputs_end_in_recoverable_quality_error(tmp_path, monkeypatch, outputs, code):
    runtime, calls = gemma_runtime(tmp_path, monkeypatch, [{"content": output} for output in outputs])
    with pytest.raises(EngineError) as error:
        await runtime.translate("Hello there.", "en")
    assert error.value.code == code and error.value.status == 422
    assert len(calls) == 2


@pytest.mark.parametrize("text", [
    "한국어와中国语", "번역: 한국어에漢字", "한국어와ひらがな", "한글カタカナ", "한글ｶﾀｶﾅ",
])
def test_mixed_hangul_and_untranslated_cjk_is_rejected(text):
    assert not korean_output(text)


@pytest.mark.parametrize("text", ["iPhone으로 시청합니다.", "YouTube에서 만나요.", "RTX 4080", "123.45", "₩10,000"])
def test_existing_latin_brands_and_numbers_stay_allowed(text):
    assert korean_output(text)


@pytest.mark.asyncio
async def test_non_gemma_model_keeps_chat_completions_transport(tmp_path, monkeypatch):
    runtime, calls = gemma_runtime(tmp_path, monkeypatch, [
        {"choices": [{"message": {"content": "안녕하세요."}, "finish_reason": "stop"}]},
    ], model="Qwen3.5-4B-Q4_K_M.gguf")
    assert await runtime.translate("Hello.", "en") == "안녕하세요."
    assert len(calls) == 1 and calls[0]["url"].endswith("/v1/chat/completions")
    assert "messages" in calls[0]["payload"] and "prompt" not in calls[0]["payload"]
