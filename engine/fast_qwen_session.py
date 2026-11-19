"""Fast Qwen's bounded speech previews and independently fed final recognition."""
from __future__ import annotations

import asyncio

from .audio import remove_forced_overlap
from .fast_qwen import FastQwenController
from .revisable_captions import RevisableCaptions
from .settings import EngineError
from .stable_sessions import drive


async def run(session):
    detector = (await asyncio.to_thread(session.runtime.create_voice_detector)
                if hasattr(session.runtime, "create_voice_detector") else None)
    controller = FastQwenController(voiced_detector=detector)
    previous = ""
    previous_generation = controller.generation
    captions = RevisableCaptions()
    last_decode_seconds = 0.0
    clock = asyncio.get_running_loop().time

    async def drain_recoveries():
        nonlocal previous, previous_generation, captions
        if previous_generation != controller.generation:
            previous = ""
            previous_generation = controller.generation
            session.break_audio_context()
            retired, captions = captions, RevisableCaptions()
            if retired.captions:
                await retired.publish(session, "", "", session.language, True, clock())
        for event in controller.pop_recoveries():
            session.report_overload(event["stage"],
                dropped_audio_seconds=event["dropped_audio_seconds"],
                dropped_items=event["dropped_items"],
                buffer_seconds=controller.buffered_seconds)

    def audio_gap(dropped_samples):
        controller.reset_for_gap(dropped_samples)

    def next_snapshot():
        # Closed windows and the near-endpoint ASR cache remain available even
        # on a slow GPU. Extra prefix inference uses an idle queue only.
        allow_preview = session.audio.bytes == 0 and session.texts.empty() and last_decode_seconds < 0.8
        return controller.next_snapshot(allow_speech_preview=allow_preview)

    async def recognize(snapshot):
        if snapshot.cached_text is not None:
            return snapshot.cached_text, snapshot.cached_language
        try:
            return await asyncio.to_thread(session.runtime.transcribe, snapshot.pcm, session.language)
        except EngineError as exc:
            if snapshot.is_final or exc.code not in {"asr_truncated", "asr_invalid_output"}:
                raise
            return "", session.language

    async def accept(snapshot, recognized, duration):
        nonlocal previous, captions, last_decode_seconds
        await drain_recoveries()
        if snapshot.cached_text is None:
            last_decode_seconds = duration
        text, language = recognized
        result = controller.accept(snapshot, text, language)
        if not result.is_final and not result.text:
            return
        current = captions
        if result.is_final:
            # Once ASR accepts a window, its pending finals must survive a gap
            # even if it occurs while sending the first sentence in this batch.
            captions = RevisableCaptions()
        for code in result.warnings:
            await session.emit({"type": "warning", "code": code,
                "message": "최종 음성 인식 결과가 비어 이전 잠정 문구를 표시하지 않았습니다. 다음 음성을 계속 처리합니다."})
        language = result.language if session.language == "auto" else session.language
        text = remove_forced_overlap(previous, result.text, language) if result.overlaps_previous else result.text
        if result.is_final:
            previous = result.text
        await current.publish(session, text, text, language, result.is_final, clock())

    await drive(session, controller, next_snapshot, recognize, accept,
                on_audio_gap=audio_gap, on_feed=drain_recoveries)
