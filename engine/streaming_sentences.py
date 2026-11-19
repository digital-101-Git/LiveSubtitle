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
    retry: bool = False


@dataclass(slots=True)
class _GrowingTail:
    anchor: str
    latest: str
    since: float


def extends_draft(older: str, newer: str) -> bool:
    """Only unchanged text ending on a word or Han/kana character boundary."""
    if not older or len(older) >= len(newer) or not newer.startswith(older):
        return False
    next_char = newer[len(older)]
    if next_char.isspace():
        return older[-1] not in "'’_-‐‑"
    if older[-1] in "'’_-‐‑" or next_char in "'’_-‐‑" or unicodedata.category(next_char).startswith("M"):
        return False
    preceding = next((ch for ch in reversed(older) if not unicodedata.category(ch).startswith("M")), "")
    if not preceding:
        return False
    # A decimal or thousands separator can change the preceding number.
    if older[-1].isdigit() and (next_char.isalnum()
                                or next_char in ".,，．" and newer[len(older) + 1:len(older) + 2].isdigit()):
        return False
    if next_char.isdigit() and older[-1] in ".,，．" and older[-2:-1].isdigit():
        return False
    if not next_char.isalnum() or not preceding.isalnum():
        return True
    return any("\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u9fff"
               for ch in (preceding, next_char))


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
        self._tail: _GrowingTail | None = None

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
            published.retry = False
        return published

    def draft_observations(self, text: str):
        """Pair this turn's live IDs with the provider's latest source at each position."""
        if self._published:
            self._validate(text)
            parts = split_sentences(text)
            for index, published in self._published.items():
                yield published.segment_id, parts[index] if index < len(parts) else ""

    def retry_draft(self, segment_id: int):
        """A withdrawn MT job may be reoffered if its source stabilizes again."""
        for published in self._published.values():
            if published.segment_id == segment_id:
                published.retry = True
                break

    def observe(self, text: str, now: float) -> list[SentenceUpdate]:
        """Replace the interim hypothesis and publish sufficiently stable parts."""
        self._validate(text)
        parts = split_sentences(text)
        tail = ""
        if parts and completed_sentence_prefix(parts[-1]) != parts[-1]:
            tail = parts.pop()
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
        same_context = prefix_unchanged and len(candidates) == len(self._candidates)
        self._candidates = candidates
        updates = self.flush_due(now)
        # Only new provider evidence can release an unpunctuated draft. Advancing
        # the timer or repeating an otherwise unchanged fragment is insufficient.
        stable = self._observe_tail(tail, now, same_context)
        if stable:
            index = len(candidates)
            previous = self._published.get(index)
            if previous is None or previous.text != stable or previous.retry:
                published = self._allocate(index, stable)
                updates.append(SentenceUpdate(published.segment_id, stable, False))
        return updates

    def _observe_tail(self, text: str, now: float, same_context: bool) -> str:
        previous = self._tail
        if not text:
            self._tail = None
            return ""
        if not same_context or previous is None or not text.startswith(previous.latest):
            self._tail = _GrowingTail(text, text, now)
            return ""
        if text == previous.latest:
            return ""
        previous.latest = text
        if not extends_draft(previous.anchor, text) or not any(c.isalnum() for c in previous.anchor):
            self._tail = _GrowingTail(text, text, now)
            return ""
        if now - previous.since < self.stability_seconds:
            return ""
        self._tail = _GrowingTail(text, text, now)
        return previous.anchor

    def flush_due(self, now: float) -> list[SentenceUpdate]:
        """Check time only; timer ticks never count as additional observations."""
        updates: list[SentenceUpdate] = []
        for index, candidate in enumerate(self._candidates):
            if (candidate.observations < self.minimum_observations
                    or now - candidate.since < self.stability_seconds):
                continue
            previous = self._published.get(index)
            if previous is not None and previous.text == candidate.text and not previous.retry:
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
        self._tail = None
        return updates


def _sentence_key(text: str) -> str:
    """Comparison only: preserve the exact source text in every emitted update."""
    return "".join(character for character in text.casefold()
                   if not character.isspace() and not unicodedata.category(character).startswith("P"))
