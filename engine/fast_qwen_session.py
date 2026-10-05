"""Legacy Qwen caption policy with independently fed endpoint/early-ASR work."""
from __future__ import annotations

import asyncio

from .audio import remove_forced_overlap
from .fast_qwen import FastQwenController
from .stable_sessions import drive


async def run(session):
    detector = (await asyncio.to_thread(session.runtime.create_voice_detector)
                if hasattr(session.runtime, "create_voice_detector") else None)
    controller = FastQwenController(voiced_detector=detector)
    previous = ""

    async def recognize(snapshot):
        if snapshot.cached_text is not None:
            return snapshot.cached_text, snapshot.cached_language
        return await asyncio.to_thread(session.runtime.transcribe, snapshot.pcm, session.language)

    async def accept(snapshot, recognized, duration):
        nonlocal previous
        text, language = recognized
        result = controller.accept(snapshot, text, language)
        if not result.is_final:
            return
        for code in result.warnings:
            await session.emit({"type": "warning", "code": code,
                "message": "최종 음성 인식 결과가 비어 이전 잠정 문구를 표시하지 않았습니다. 다음 음성을 계속 처리합니다."})
        language = result.language if session.language == "auto" else session.language
        text = remove_forced_overlap(previous, result.text, language) if result.overlaps_previous else result.text
        previous = result.text
        await session._final(text, language)

    await drive(session, controller, controller.next_snapshot, recognize, accept)
