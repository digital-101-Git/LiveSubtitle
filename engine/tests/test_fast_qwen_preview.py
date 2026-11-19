import asyncio

import pytest

from engine.fast_qwen import FastQwenController
from engine.tests.test_fast_qwen_session import Runtime, pcm, session_for


def test_prefix_decode_never_replaces_a_four_second_final():
    controller = FastQwenController()
    audio = pcm(1.4) + pcm(2.6, 1700)
    for i in range(0, len(audio), 3200):
        controller.feed(audio[i:i + 3200])
        snapshot = controller.next_snapshot(allow_speech_preview=True)
        if snapshot and not snapshot.is_final:
            assert snapshot.reason == "speech_preview" and len(snapshot.pcm) == 44800
            preview = controller.accept(snapshot, "Perhaps", "en")
            assert not preview.is_final and preview.text == "Perhaps"
        elif snapshot:
            assert snapshot.pcm == audio and snapshot.cached_text is None
            final = controller.accept(snapshot, "Perhaps not.", "en")
            assert final.is_final and final.text == "Perhaps not."
    assert controller.statistics["maximum_endpoints"] == 1


def test_pending_final_wins_over_a_late_speech_preview():
    controller = FastQwenController()
    controller.feed(pcm(1))
    controller.feed(pcm(.4))
    pending = controller.next_snapshot(allow_speech_preview=True)
    tail = pcm(.2, 1700) + pcm(.5, 0)
    controller.feed(tail)
    assert not controller.accept(pending, "Old hypothesis", "en").text
    final = controller.next_snapshot(allow_speech_preview=True)
    assert final.is_final and final.cached_text is None and final.pcm == pcm(1.4) + tail


async def wait_event(socket, predicate):
    async def search():
        while True:
            for event in socket.events:
                if predicate(event):
                    return event
            await asyncio.sleep(.005)
    return await asyncio.wait_for(search(), 3)


@pytest.mark.asyncio
async def test_fast_qwen_captions_while_speaking_and_corrects_same_id():
    runtime = Runtime()
    runtime.release.set()
    session, socket = session_for(runtime)
    await session.start()
    try:
        await session.feed(pcm(.7))
        await session.feed(pcm(.7))
        preview = await wait_event(socket, lambda e: e["type"] == "caption" and not e["is_final"])
        assert preview["source_text"] == "日本語。" and preview["language"] == "ja"
        await session.feed(pcm(.5, 1700) + pcm(.5, 0))
        final = await wait_event(socket, lambda e: e["type"] == "caption" and e["is_final"])
        assert final["segment_id"] == preview["segment_id"] and final["source_text"] == "続きの日本語。"
        assert [a for a, _ in runtime.calls] == [pcm(1.4), pcm(1.4) + pcm(.5, 1700) + pcm(.5, 0)]
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_fast_qwen_retracts_published_draft_at_gap_without_new_voice():
    runtime = Runtime()
    runtime.release.set()
    session, socket = session_for(runtime)
    await session.start()
    try:
        await session.feed(pcm(.7))
        await session.feed(pcm(.7))
        old = await wait_event(socket, lambda e: e["type"] == "caption" and not e["is_final"])
        session.input_gap(160)
        await session.feed(pcm(.1, 0))
        removed = await wait_event(socket, lambda e: e["type"] == "caption_remove")
        assert removed["segment_id"] == old["segment_id"]
        await session.feed(pcm(.26, 2000) + pcm(.5, 0))
        new = await wait_event(socket, lambda e: e["type"] == "caption" and e["is_final"])
        assert new["segment_id"] != old["segment_id"]
    finally:
        await session.stop()
