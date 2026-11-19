"""The public raw preview can contain several clauses; outstanding MT belongs to one."""
import asyncio
from types import SimpleNamespace

import pytest

from engine.revisable_captions import RevisableCaptions
from engine.sessions import SessionManager, StreamSession


@pytest.mark.asyncio
@pytest.mark.parametrize("new_tail,expect_old", [
    ("I cannot swim", False), ("I can swim", True)])
async def test_throttled_multisentence_observation_rechecks_only_its_clause(new_tail, expect_old):
    entered = asyncio.Event()
    release = asyncio.Event()
    events = []

    async def send_json(event):
        events.append(event)

    async def translate(source, language, context, *, target_language):
        if source == "I can":
            entered.set()
            await release.wait()
        return "번역 " + source

    session = StreamSession(SimpleNamespace(send_json=send_json), SimpleNamespace(translate=translate),
                            SessionManager(), SimpleNamespace(data={}), "local", "en")
    session.active = True
    captions = RevisableCaptions()
    worker = asyncio.create_task(session._translate())
    try:
        source = "Listen. I can"
        await captions.publish(session, source, source, "en", False, 10.0)
        await asyncio.wait_for(entered.wait(), 3)
        source = "Listen. " + new_tail
        await captions.publish(session, source, source, "en", False, 10.1)
        release.set()
        await asyncio.wait_for(session.texts.join(), 3)
        heard = [e["source_text"] for e in events if e["type"] == "caption"]
        assert ("I can" in heard) == expect_old
        await captions.publish(session, source, source, "en", True, 11.0)
        await asyncio.wait_for(session.texts.join(), 3)
        assert any(e.get("source_text") == new_tail and e["is_final"] for e in events)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
