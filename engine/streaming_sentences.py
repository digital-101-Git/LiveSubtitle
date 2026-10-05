"""Stable provisional sentence updates reconciled with authoritative finals."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import unicodedata

from .sentences import completed_sentence_prefix, split_sentences
from .settings import EngineError


@dataclass(frozen=True, slots=True)
class SentenceUpdate:
    segment_id: int
    text: str
    is_final: bool
    removed: bool = False


@dataclass(slots=True)
class _Candidate:
    text: str
    since: float
    observations: int


@dataclass(slots=True)
class _Published:
    segment_id: int
    text: str


class StreamingSentences:
    """Track cumulative interim hypotheses for one turn at a time.

    Interim positions identify captions. Ordered sentence alignment preserves
    existing IDs when finalization inserts or deletes an earlier sentence.
    Finalization publishes all authoritative text, including unfinished fragments.
    """

    stability_seconds = 0.6
    minimum_observations = 2

    def __init__(self):
        self.nextid = 1
        self._candidates: list[_Candidate] = []
        self._published: dict[int, _Published] = {}

    @staticmethod
    def _validate(text: str) -> None:
        if len(text) > 6000:
            raise EngineError("transcript_too_long", "음성 구간이 너무 깁니다. 자막을 다시 시작해 주세요.")

    def _allocate(self, index: int, text: str) -> _Published:
        published = self._published.get(index)
        if published is None:
            published = _Published(self.nextid, text)
            self.nextid += 1
            self._published[index] = published
        else:
            published.text = text
        return published

    def observe(self, text: str, now: float) -> list[SentenceUpdate]:
        """Replace the interim hypothesis and publish sufficiently stable parts."""
        self._validate(text)
        parts = split_sentences(text)
        if parts and completed_sentence_prefix(parts[-1]) != parts[-1]:
            parts.pop()
        candidates: list[_Candidate] = []
        prefix_unchanged = True
        for index, sentence in enumerate(parts):
            previous = self._candidates[index] if index < len(self._candidates) else None
            # Once an earlier position changed, every following prefix changed.
            # This avoids storing quadratic copies of long sentence prefixes.
            prefix_unchanged = prefix_unchanged and previous is not None and previous.text == sentence
            if prefix_unchanged:
                candidate = _Candidate(sentence, previous.since, previous.observations + 1)
            else:
                candidate = _Candidate(sentence, now, 1)
            candidates.append(candidate)
        self._candidates = candidates
        return self.flush_due(now)

    def flush_due(self, now: float) -> list[SentenceUpdate]:
        """Check time only; timer ticks never count as additional observations."""
        updates: list[SentenceUpdate] = []
        for index, candidate in enumerate(self._candidates):
            if (candidate.observations < self.minimum_observations
                    or now - candidate.since < self.stability_seconds):
                continue
            previous = self._published.get(index)
            if previous is not None and previous.text == candidate.text:
                continue
            published = self._allocate(index, candidate.text)
            updates.append(SentenceUpdate(published.segment_id, candidate.text, False))
        return updates

    def finalize(self, text: str) -> list[SentenceUpdate]:
        """Align the complete final in order, retract extras, and start a turn."""
        self._validate(text)
        parts = split_sentences(text)
        previous = [(published.segment_id, published.text)
                    for _, published in sorted(self._published.items())]
        matcher = SequenceMatcher(
            None, [_sentence_key(source) for _, source in previous],
            [_sentence_key(sentence) for sentence in parts], autojunk=False,
        )
        identifiers: list[int | None] = [None] * len(parts)
        retained: set[int] = set()
        for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if operation in ("equal", "replace"):
                # Equal blocks keep their IDs even after an insertion/deletion.
                # A changed block maps revisions in order without borrowing IDs
                # from later equal blocks; surplus sentences remain insertions.
                for old_index, new_index in zip(range(old_start, old_end), range(new_start, new_end)):
                    segment_id = previous[old_index][0]
                    identifiers[new_index] = segment_id
                    retained.add(segment_id)
        updates: list[SentenceUpdate] = []
        for index, sentence in enumerate(parts):
            segment_id = identifiers[index]
            if segment_id is None:
                segment_id = self.nextid
                self.nextid += 1
            updates.append(SentenceUpdate(segment_id, sentence, True))
        for segment_id, source in previous:
            if segment_id not in retained:
                updates.append(SentenceUpdate(segment_id, source, True, removed=True))
        self._candidates = []
        self._published = {}
        return updates


def _sentence_key(text: str) -> str:
    """Comparison only: preserve the exact source text in every emitted update."""
    return "".join(character for character in text.casefold()
                   if not character.isspace() and not unicodedata.category(character).startswith("P"))
