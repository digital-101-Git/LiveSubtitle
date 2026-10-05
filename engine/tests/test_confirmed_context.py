"""Only final, successful source text may condition later subtitle translation."""
from types import SimpleNamespace
import pytest

from engine.sessions import SessionManager, StreamSession
from engine.streaming_sentences import SentenceUpdate
from engine.settings import EngineError


class Socket:
    async def send_json(self, event):
        pass


class Translator:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def translate(self, text, language, context, *, target_language="ko"):
        self.calls.append((text, list(context)))
        if self.fail:
            raise EngineError("translation_language", "test failure", 422)
        return "한국어 번역"


def session():
    runtime = Translator()
    stream = StreamSession(Socket(), runtime, SessionManager(), SimpleNamespace(data={}), "gemini", "zh")
    stream.active = True
    return stream, runtime


async def translate_pending(stream):
    while not stream.texts.empty():
        segment, work = stream.texts.get_nowait()
        await stream._translate_sentence(segment, work)
        stream.texts.task_done()


@pytest.mark.asyncio
async def test_provisional_success_is_excluded_until_same_source_final():
    stream, runtime = session()
    await stream._apply_sentences([SentenceUpdate(1, "临时原文。", False)])
    await translate_pending(stream)
    assert stream._context == {}
    await stream._apply_sentences([SentenceUpdate(2, "下一句。", False)])
    await translate_pending(stream)
    assert runtime.calls[-1][1] == []
    await stream._apply_sentences([SentenceUpdate(1, "临时原文。", True)])
    assert stream._context == {1: "临时原文。"}
    assert len(runtime.calls) == 2  # Promotion reuses its successful translation.
    await stream._apply_sentences([SentenceUpdate(3, "最终句子。", True)])
    await translate_pending(stream)
    assert runtime.calls[-1][1] == ["临时原文。"]


@pytest.mark.asyncio
async def test_revised_or_retracted_source_is_not_used_as_context():
    stream, runtime = session()
    await stream._apply_sentences([SentenceUpdate(1, "错误版本。", False)])
    await translate_pending(stream)
    await stream._apply_sentences([SentenceUpdate(1, "修正版本。", True)])
    await translate_pending(stream)
    await stream._apply_sentences([SentenceUpdate(2, "继续。", True)])
    await translate_pending(stream)
    assert runtime.calls[-1][1] == ["修正版本。"]
    await stream._apply_sentences([SentenceUpdate(1, "修正版本。", True, removed=True)])
    assert 1 not in stream._context


@pytest.mark.asyncio
async def test_only_latest_three_successful_sources_in_source_order():
    stream, runtime = session()
    for ident in range(1, 6):
        await stream._apply_sentences([SentenceUpdate(ident, f"原文{ident}。", True)])
        await translate_pending(stream)
    assert runtime.calls[-1][1] == ["原文2。", "原文3。", "原文4。"]
    runtime.fail = True
    await stream._apply_sentences([SentenceUpdate(6, "翻译失败。", True)])
    await translate_pending(stream)
    assert 6 not in stream._context
    assert all("한국어" not in source for source in stream._context.values())
