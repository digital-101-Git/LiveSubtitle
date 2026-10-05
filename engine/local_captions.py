"""Bounded provisional captions for the existing local ASR controller.

This adapter never commits recognition or changes PCM. Authoritative segments
come only from StreamingResult.segments; pending text may replace one preview.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .local_streaming import ASRSnapshot, StreamingResult
from .sentences import completed_sentence_prefix
from .streaming_sentences import SentenceUpdate


_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


@dataclass(slots=True)
class _Preview:
    segment_id: int
    text: str
    retry: bool = False
    accepted: bool = False


class LocalCaptions:
    """Map one revisable preview and ordered commits to session-wide IDs.

    Feed each accepted snapshot once, in sequence order. ``now`` can supply a
    monotonic wall clock to additionally throttle backlog processing; without
    it, the immutable snapshot's audio clock supplies deterministic timing.
    """

    minimum_audio_seconds = 1.2
    update_interval_seconds = .8
    maximum_preview_characters = 96
    short_preview_stability_seconds = .35

    def __init__(self, start_id: int = 1):
        if not isinstance(start_id, int) or isinstance(start_id, bool) or start_id < 1:
            raise ValueError("start_id must be a positive integer")
        self.nextid = start_id
        self._window_id: int | None = None
        self._window_start = 0.0
        self._last_sequence = 0
        self._closed = False
        self._preview: _Preview | None = None
        self._last_preview_audio: float | None = None
        self._last_preview_time: float | None = None
        self._candidate_text = ""
        self._candidate_audio = 0.0
        self._candidate_time = 0.0

    def _clear_candidate(self) -> None:
        self._candidate_text = ""

    def _new_preview_ready(self, text: str, audio_end: float, clock: float) -> bool:
        # Very short unpunctuated fragments can still be the start of a noun
        # phrase. Observe them twice before starting a new translation, without
        # trying to infer sentence meaning from a language-specific word list.
        # A final commit always bypasses this guard, even for one spoken letter.
        alphanumeric = sum(character.isalnum() for character in text)
        short = (alphanumeric <= 6 if _CJK.search(text) else
                 len(text.split()) <= 2 and alphanumeric <= 24)
        if not short or completed_sentence_prefix(text) or text.endswith(("，", ",", "、", "；", ";")):
            self._clear_candidate()
            return True
        if text != self._candidate_text:
            self._candidate_text = text
            self._candidate_audio = audio_end
            self._candidate_time = clock
            return False
        return (audio_end - self._candidate_audio >= self.short_preview_stability_seconds - 1e-9
                and clock - self._candidate_time >= self.short_preview_stability_seconds - 1e-9)

    def _allocate(self) -> int:
        identifier = self.nextid
        self.nextid += 1
        return identifier

    def _remove_preview(self, updates: list[SentenceUpdate]) -> None:
        if self._preview is not None:
            updates.append(SentenceUpdate(self._preview.segment_id,
                                          self._preview.text, True, removed=True))
            self._preview = None

    def defer_preview(self, segment_id: int, text: str) -> None:
        """Retry an offered preview that the translation queue could not take.

        A delayed rejection must not revive a removed/finalized preview or
        overwrite a newer hypothesis. Reoffers keep the ordinary audio and
        wall-clock throttle; an accepted offer needs no acknowledgement.
        """
        if (self._preview is not None and self._preview.segment_id == segment_id
                and self._preview.text == text):
            self._preview.retry = True

    def _preview_text(self, pending: str) -> str:
        text = pending.strip()
        if not any(character.isalnum() for character in text):
            return ""
        limit = self.maximum_preview_characters
        sentence = completed_sentence_prefix(text)
        if sentence and len(sentence) <= limit:
            return sentence
        # Prefer a short clause over repeatedly translating a growing buffer.
        for index, character in enumerate(text[:limit]):
            if character in "，、；;" or (character == "," and
                    not (text[index - 1:index].isdigit() and text[index + 1:index + 2].isdigit())):
                prefix = text[:index + 1]
                if any(letter.isalnum() for letter in prefix):
                    return prefix
            if (character.isspace() and index >= 6
                    and _CJK.fullmatch(text[index - 1:index])
                    and _CJK.match(text[index:].lstrip())):
                return text[:index].rstrip()
        if len(text) <= limit:
            return text
        end = limit
        # Keep an ordinary Latin word/number intact when a preceding space is
        # available. A single exceptionally long token is bounded as a preview;
        # its complete text is still delivered by the authoritative commit.
        if (text[end - 1:end].isascii() and text[end - 1:end].isalnum()
                and text[end:end + 1].isascii() and text[end:end + 1].isalnum()):
            space = text.rfind(" ", 0, end)
            if space > 0:
                end = space
        return text[:end].rstrip()

    def accept(self, snapshot: ASRSnapshot, result: StreamingResult,
               now: float | None = None) -> list[SentenceUpdate]:
        """Return revisions, ordered final captions, and preview removals.

        Final captions bypass preview timing/length limits. Only a preview can
        be removed, so a later empty hypothesis cannot erase committed speech.
        """
        if snapshot.sequence <= self._last_sequence:
            return []
        self._last_sequence = snapshot.sequence
        # The session applies each offer and calls defer_preview before feeding
        # the next snapshot. No rejection therefore acknowledges the previous
        # offer. Keep this across later deferred corrections of an accepted ID.
        if self._preview is not None and not self._preview.retry:
            self._preview.accepted = True
        updates: list[SentenceUpdate] = []
        if snapshot.window_id != self._window_id:
            self._remove_preview(updates)
            self._clear_candidate()
            self._window_id = snapshot.window_id
            self._window_start = snapshot.offset_seconds
            self._closed = False
        elif self._closed:
            return []

        audio_end = snapshot.offset_seconds + len(snapshot.pcm) / 32000
        clock = audio_end if now is None else now
        for segment in result.segments:
            text = segment.strip()
            if not text:
                continue
            # The pending text after a commit is a new residual phrase. Its
            # stability cannot be borrowed from the just-committed hypothesis.
            self._clear_candidate()
            if self._preview is not None:
                identifier = self._preview.segment_id
                self._preview = None
            else:
                identifier = self._allocate()
            updates.append(SentenceUpdate(identifier, text, True))

        if snapshot.is_final:
            self._remove_preview(updates)
            self._clear_candidate()
            self._closed = True
            return updates

        text = self._preview_text(result.pending_text)
        if not text:
            self._remove_preview(updates)
            self._clear_candidate()
            return updates
        needs_initial_guard = (self._preview is None or
                               (self._preview.retry and not self._preview.accepted))
        if needs_initial_guard and not self._new_preview_ready(text, audio_end, clock):
            return updates
        if self._preview is not None and self._preview.text == text and not self._preview.retry:
            return updates
        if audio_end - self._window_start < self.minimum_audio_seconds - 1e-9:
            return updates
        if self._last_preview_audio is not None:
            if (audio_end - self._last_preview_audio < self.update_interval_seconds - 1e-9
                    or clock - self._last_preview_time < self.update_interval_seconds - 1e-9):
                return updates
        if self._preview is None:
            self._preview = _Preview(self._allocate(), text)
        else:
            self._preview.text = text
            self._preview.retry = False
        self._last_preview_audio = audio_end
        self._last_preview_time = clock
        updates.append(SentenceUpdate(self._preview.segment_id, text, False))
        return updates
