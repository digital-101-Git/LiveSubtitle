"""Translation recovery contracts. Responses are local mocks; no model/API calls."""
import copy
import json

import httpx
import pytest

from engine.runtime import Runtime, korean_output
from engine.settings import EngineError


@pytest.mark.parametrize("text", [
    "네.", "음, 그렇군요.", "존 레넌", "RTX 4080", "NASA", "60 FPS", "OK",
    "RTX 4080으로 실행해요.", "123.45", "-3.5%", "₩10,000", "…?!", "♪",
    "１２３。", "안녕", "반가워요 😀", "iPhone으로 봐요.",
    "YouTube와 Twitch에서 만나요.", "안녕, John!", "MacBook Pro를 샀어요.",
])
def test_korean_numbers_symbols_and_short_acronyms_remain_allowed(text):
    assert korean_output(text)


@pytest.mark.parametrize("text", [
    "", " \n", "Mhm", "John Lennon", "The next match begins.", "次の試合です。",
    "比赛开始了。", "번역: The next match begins.", "번역: 次の試合です。",
    "번역: 比赛开始了。", "Привет",
])
def test_foreign_text_is_not_accepted_as_korean(text):
    assert not korean_output(text)


def mock_runtime(tmp_path, monkeypatch, replies):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, json):
            calls.append(copy.deepcopy(json))
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            if isinstance(reply, dict):
                body = reply
            else:
                body = {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr("engine.runtime.httpx.AsyncClient", Client)
    runtime = Runtime(tmp_path)
    runtime.llama_url = "http://127.0.0.1:1"
    monkeypatch.setattr(runtime, "status", lambda: {"translation_ready": True})
    return runtime, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("source,first,translated", [
    ("Mhm.", "Mhm.", "음."), ("John Lennon.", "John Lennon.", "존 레넌."),
    ("そうですね。", "そうですね。", "그렇네요."),
    ("Yes.", "", "네."), ("Yes.", None, "네."),
    ("Yes.", "<think>analysis only</think>", "네."),
    ("Yes.", "<think>unfinished analysis", "네."),
    ("Hmm.", "…", "흠."),
])
async def test_bad_output_retries_fresh_without_failed_answer_or_prior_context(
        tmp_path, monkeypatch, source, first, translated):
    runtime, calls = mock_runtime(tmp_path, monkeypatch, [first, translated])
    assert await runtime.translate(source, "auto", ["prior unrelated speech"]) == translated
    assert len(calls) == 2
    assert json.loads(calls[0]["messages"][1]["content"])["context"] == ["prior unrelated speech"]
    retry = calls[1]
    assert [message["role"] for message in retry["messages"]] == ["system", "user"]
    assert retry["messages"][0]["content"] != calls[0]["messages"][0]["content"]
    assert "한글 발음" in retry["messages"][0]["content"]
    request = json.loads(retry["messages"][1]["content"])
    assert request["current_text"] == source and "context" not in request
    assert retry["temperature"] == 0 and retry["chat_template_kwargs"]["enable_thinking"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("replies,code", [
    (["Mhm", "Still English"], "translation_language"),
    (["", None], "translation_empty"),
    (["<think>reasoning</think>", "<think>unfinished"], "translation_empty"),
    (["Wrong language", ""], "translation_empty"),
    (["", "Wrong language"], "translation_language"),
    (["…", "..."], "translation_empty"),
    (["번역: …", "한국어: ..."], "translation_empty"),
])
async def test_repeated_bad_output_is_bounded_recoverable_quality_error(tmp_path, monkeypatch, replies, code):
    runtime, calls = mock_runtime(tmp_path, monkeypatch, replies)
    with pytest.raises(EngineError) as error:
        await runtime.translate("Mhm.", "en")
    assert error.value.code == code
    assert error.value.status == 422
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    httpx.ConnectError("mock disconnected"),
    {"choices": []}, {"choices": [{"message": {"content": ["bad schema"]}}]},
])
async def test_transport_and_invalid_schema_remain_engine_failures(tmp_path, monkeypatch, reply):
    runtime, calls = mock_runtime(tmp_path, monkeypatch, [reply])
    with pytest.raises(EngineError) as error:
        await runtime.translate("Hello", "en")
    assert error.value.code == "translation_failed" and len(calls) == 1


@pytest.mark.asyncio
async def test_truncation_is_not_misreported_as_language_error(tmp_path, monkeypatch):
    runtime, calls = mock_runtime(tmp_path, monkeypatch, [{"choices": [{
        "message": {"content": "잘린 번역"}, "finish_reason": "length"}]}])
    with pytest.raises(EngineError) as error:
        await runtime.translate("long speech", "en")
    assert error.value.code == "translation_truncated" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["반가워요.", "RTX 4080", "-2.5%", "iPhone으로 봐요."])
async def test_valid_output_returns_without_retry(tmp_path, monkeypatch, output):
    runtime, calls = mock_runtime(tmp_path, monkeypatch, [output])
    assert await runtime.translate("source", "auto") == output
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["…", "...", "?!", " ♪ "])
async def test_punctuation_only_source_preserved_without_a_model_request(tmp_path, monkeypatch, source):
    runtime, calls = mock_runtime(tmp_path, monkeypatch, [])
    assert await runtime.translate(source, "auto") == source.strip()
    assert calls == []
