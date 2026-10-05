"""Align final sentence revisions without changing IDs of retained speech."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .qwen_streaming import _key


@dataclass(frozen=True, slots=True)
class SentenceRevision:
    captions: tuple[tuple[int, str], ...]
    removed: tuple[tuple[int, str], ...]
    last_id: int


def reconcile(previous, replacements, last_id: int) -> SentenceRevision:
    """Match ordered occurrences, never deduplicate actually repeated text.

    Qwen's existing comparison key tolerates CJK spacing/script and punctuation
    changes while retaining decimal points and Latin word boundaries. Equal
    blocks keep their IDs but carry the authoritative original spelling.
    """
    previous, replacements = tuple(previous), tuple(replacements)
    output, removed = [], []
    matcher = SequenceMatcher(None, [_key(text) for _, text in previous],
                              [_key(text) for text in replacements], autojunk=False)
    for kind, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        old = previous[old_start:old_end]
        new = replacements[new_start:new_end]
        if kind == "equal":
            output.extend((identifier, text) for (identifier, _), text in zip(old, new))
            continue
        if kind == "delete":
            removed.extend(old)
            continue
        if kind == "replace":
            paired = min(len(old), len(new))
            output.extend((old[index][0], new[index]) for index in range(paired))
            removed.extend(old[paired:])
            new = new[paired:]
        for text in new:
            last_id += 1
            output.append((last_id, text))
    return SentenceRevision(tuple(output), tuple(removed), last_id)
