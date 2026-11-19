"""Publish revisable translations from cumulative ASR text, retaining sentence IDs."""
from .local_captions import LocalCaptions
from .qwen_revisions import reconcile
from .sentences import split_sentences
from .settings import EngineError
from .streaming_sentences import SentenceUpdate


class RevisableCaptions:
    """Offer cumulative stable text while speaking. The endpoint owns final text."""

    def __init__(self):
        self.captions = ()
        self.delivered = {}
        self.last_offer = float("-inf")
        self.last_transcript = ""

    async def publish(self, session, hypothesis, stable, language, final, now):
        source = (hypothesis if final else stable).strip()
        if len(source) > 6000:
            raise EngineError("transcript_too_long", "음성 구간이 너무 깁니다. 자막을 다시 시작해 주세요.")

        # The recognizer decides which text is suitable for a preview. Throttle
        # translation revisions without delaying a definitive result.
        revision = reconcile(self.captions, split_sentences(source), session.segment)
        # A raw preview can span several sentences. Check translations against
        # their own clause even when this observation does not enqueue new MT.
        for identifier, text in revision.captions:
            if identifier <= session.segment and session.observe_draft(identifier, text, language):
                self.delivered.pop(identifier, None)
        for identifier, _ in revision.removed:
            if session.observe_draft(identifier, "", language):
                self.delivered.pop(identifier, None)
        if not final and (not source or now - self.last_offer < LocalCaptions.update_interval_seconds):
            await self._transcript(session, source)
            return
        session.segment = revision.last_id
        updates = [SentenceUpdate(identifier, text, True, removed=True)
                   for identifier, text in revision.removed]
        for index, (identifier, text) in enumerate(revision.captions):
            session._sentence_order[identifier] = (session._turn, index)
            if final or self.delivered.get(identifier) != (text, language):
                updates.append(SentenceUpdate(identifier, text, final))
        self.captions = revision.captions
        if updates:
            self.last_offer = now
            deferred = await session._apply_sentences(updates, language)
            self.delivered = {identifier: (text, language) for identifier, text in revision.captions}
            for update in deferred:
                self.delivered.pop(update.segment_id, None)

        if final:
            session._turn += 1
            self.captions = ()
            self.delivered.clear()
            self.last_transcript = ""
            self.last_offer = float("-inf")
        elif updates and updates[-1].text == source and not updates[-1].removed:
            self.last_transcript = source  # _apply_sentences also sends the transcript.
        else:
            await self._transcript(session, source)
        return revision.captions

    async def _transcript(self, session, text):
        if text and text != self.last_transcript and session.active:
            self.last_transcript = text
            identifier = self.captions[-1][0] if self.captions else session.segment + 1
            await session.emit({"type": "transcript", "segment_id": identifier, "text": text,
                                "is_final": False, "preview": True})
