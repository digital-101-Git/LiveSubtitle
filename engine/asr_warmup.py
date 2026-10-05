"""Bounded, disposable ASR preparation; no recorded/user audio or transcript output."""
from __future__ import annotations

import asyncio

SAMPLE_RATE = 16000
WARMUP_TOKENS = 2


async def finish_thread_before_cancel(function, *args):
    """Keep the caller's lifecycle lock until its native worker really finishes.

    Cancelling to_thread alone does not stop CUDA/CTranslate2. Shield the worker,
    drain it (also under repeated cancellation), then propagate cancellation.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # Retrieve an eventual worker error without replacing cancellation.
        if not task.cancelled():
            task.exception()
        raise


def warmup_whisper(model) -> None:
    import numpy as np

    # VAD and no-speech rejection would otherwise skip the actual CUDA path.
    # Explicit English avoids language detection; no user hint/context is used.
    segments, _ = model.transcribe(
        np.zeros(SAMPLE_RATE, dtype=np.float32), language="en", task="transcribe",
        beam_size=5, temperature=0.0, condition_on_previous_text=False,
        vad_filter=False, no_speech_threshold=None, log_prob_threshold=None,
        compression_ratio_threshold=None, word_timestamps=False,
        max_new_tokens=WARMUP_TOKENS,
    )
    # faster-whisper is lazy: obtaining the iterator does not run inference.
    for _ in segments:
        pass


def warmup_alignatt(adapter) -> None:
    # This stream is never shared with a session. Its context/KV hooks are
    # cleared on close even if inference fails; the borrowed CT2 model remains.
    stream = adapter.create_stream("en")
    try:
        stream.feed(bytes(SAMPLE_RATE * 2), final=True)
    finally:
        stream.close()
