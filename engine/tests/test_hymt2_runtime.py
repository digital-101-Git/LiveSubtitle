"""HY-MT2 runtime contracts; HTTP is mocked and no model is loaded."""
import copy

import httpx
import pytest

from engine.runtime import Runtime
from engine.settings import EngineError


def chat_reply(content, *, finish_reason="stop", **message_fields):
    return {"choices": [{
        "message": {"content": content, **message_fields},
        "finish_reason": finish_reason,
    }]}


def hymt_runtime(tmp_path, monkeypatch, replies, model="HY-MT2-7B-Q6_K.gguf"):
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
async def test_hymt2_uses_user_only_source_and_confirmed_context(tmp_path, monkeypatch, source, language):
    from engine.translation_profiles import build_hymt2_messages

    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [chat_reply("곧 다음 경기가 시작됩니다.")])
    context = ["CONFIRMED_CONTEXT_SENTINEL"]
    result = await runtime.translate(source, language, context)
    assert result == "곧 다음 경기가 시작됩니다."
    assert len(calls) == 1 and calls[0]["url"].endswith("/v1/chat/completions")
    payload = calls[0]["payload"]
    assert payload["messages"] == build_hymt2_messages(source, language, context=context)
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "user"
    prompt = payload["messages"][0]["content"]
    assert source in prompt
    assert any(target in prompt for target in ("Korean", "韩语", "한국어"))
    assert "CONFIRMED_CONTEXT_SENTINEL" in prompt
    assert '"current_text"' not in prompt and '"source_language"' not in prompt
    assert '"context"' not in prompt and "prompt" not in payload
    assert payload["temperature"] == 0.7 and payload["top_p"] == 0.6
    assert payload["top_k"] == 20 and payload["repeat_penalty"] == 1.05
    assert payload["min_p"] == 0
    assert payload["stop"] == ["<|extra_5|>", "<|eos|>"]
    assert payload["max_tokens"] == 320 and payload["stream"] is False
    assert "chat_template_kwargs" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["Still English.", "", " \n\t", "<think>unfinished", "…", "번역: ..."])
async def test_quality_retry_keeps_bounded_official_payload(tmp_path, monkeypatch, first):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [chat_reply(first), chat_reply("네, 맞습니다.")])
    assert await runtime.translate("Yes, that's right.", "en", ["CONTEXT_SENTINEL"]) == "네, 맞습니다."
    assert len(calls) == 2
    assert calls[0]["payload"] == calls[1]["payload"]
    for call in calls:
        assert call["url"].endswith("/v1/chat/completions")
        assert call["payload"]["temperature"] == 0.7
        assert len(call["payload"]["messages"]) == 1
        assert "CONTEXT_SENTINEL" in call["payload"]["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ko", "auto"])
async def test_korean_source_returns_without_request(tmp_path, monkeypatch, language):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [])
    assert await runtime.translate("안녕하세요. 오늘 방송입니다.", language) == "안녕하세요. 오늘 방송입니다."
    assert calls == []


@pytest.mark.asyncio
async def test_reasoning_content_is_never_displayed(tmp_path, monkeypatch):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [chat_reply(
        "안녕하세요.", reasoning_content="PRIVATE_REASONING_SENTINEL 중국어 분석입니다.",
    )])
    assert await runtime.translate("Hello.", "en") == "안녕하세요."
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_closed_think_block_is_removed_from_content(tmp_path, monkeypatch):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [chat_reply(
        "<think>PRIVATE_REASONING_SENTINEL\n需要翻译。</think>\n안녕하세요.",
    )])
    assert await runtime.translate("Hello.", "en") == "안녕하세요."
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reasoning_only_response_is_not_a_translation(tmp_path, monkeypatch):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [
        chat_reply("", reasoning_content="한국어 추론만 있습니다."),
        chat_reply("", reasoning_content="또 다른 한국어 추론입니다."),
    ])
    with pytest.raises(EngineError) as error:
        await runtime.translate("Hello.", "en")
    assert error.value.code == "translation_empty" and error.value.status == 422
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    {}, {"choices": []}, {"choices": None}, {"choices": [{"message": {}}]},
    {"choices": [{"message": None}]}, chat_reply(None), chat_reply([]),
    chat_reply({"text": "잘못된 구조"}), [], None, 5,
    httpx.Response(200, content=b"invalid json", request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")),
    httpx.Response(503, json={"error": "mock unavailable"}, request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")),
    httpx.ConnectError("mock disconnected"),
])
async def test_bad_schema_and_transport_are_503_failures(tmp_path, monkeypatch, reply):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [reply])
    with pytest.raises(EngineError) as error:
        await runtime.translate("Hello.", "en")
    assert error.value.code == "translation_failed" and error.value.status == 503
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_length_stop_is_a_422_failure_without_retry(tmp_path, monkeypatch):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [chat_reply("잘린 번역입니다", finish_reason="length")])
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
async def test_two_invalid_outputs_end_in_quality_error(tmp_path, monkeypatch, outputs, code):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [chat_reply(output) for output in outputs])
    with pytest.raises(EngineError) as error:
        await runtime.translate("Hello there.", "en")
    assert error.value.code == code and error.value.status == 422
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("model,reply,endpoint", [
    ("translategemma-12b-it-Q4_K_M.gguf", {"content": "안녕하세요."}, "/completion"),
    ("Qwen3.5-4B-Q4_K_M.gguf", chat_reply("안녕하세요."), "/v1/chat/completions"),
])
async def test_other_models_keep_existing_routes(tmp_path, monkeypatch, model, reply, endpoint):
    runtime, calls = hymt_runtime(tmp_path, monkeypatch, [reply], model=model)
    assert await runtime.translate("Hello.", "en") == "안녕하세요."
    assert len(calls) == 1 and calls[0]["url"].endswith(endpoint)
    payload = calls[0]["payload"]
    assert "top_k" not in payload and "repeat_penalty" not in payload
    if model.startswith("Qwen"):
        assert payload["messages"][0]["role"] == "system"
    else:
        assert "prompt" in payload and "messages" not in payload
