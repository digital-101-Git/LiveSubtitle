"""Conservative sentence boundaries for final text and live endpoint hints.

This is deliberately a punctuation heuristic, not a language detector. Ambiguous
English abbreviations and ellipses stay with the following text; final callers
still receive every character in the unfinished remainder.
"""
from __future__ import annotations

import re
from collections.abc import Iterator


_PUNCTUATION = frozenset(".?!。！？…")
_STRONG_END = frozenset("?!。！？")
_CLOSERS = frozenset("\"'’”»›)]}」』】）》〉〕〗〙〛")
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs",
    "etc", "fig", "no", "vol", "inc", "ltd", "co", "dept", "approx",
    "est", "cf", "rev", "hon", "gen", "capt", "sgt", "lt", "col",
})
_WORD_BEFORE_DOT = re.compile(r"([A-Za-z]+)\.$")
_DOTTED_ABBREVIATION = re.compile(r"(?<![A-Za-z])(?:[A-Za-z]\.){2,}$")
# Protect URL query punctuation as well as domain dots. A trailing full stop
# conventionally belongs to the surrounding sentence. A URL's '?' and '!' may
# instead belong to its query/path, so those ambiguous marks remain protected.
# CJK sentence punctuation also terminates a token.
_WEB_TOKEN = re.compile(
    r"(?<![A-Z0-9_@])(?:"
    r"(?:https?://|www\.)[^\s<>\"'，。！？「」『』（）【】]+"
    r"|(?:[A-Z0-9._%+-]+@)?(?:[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?\.)+"
    r"[A-Z]{2,63}(?::[0-9]{1,5})?(?:[/#?][^\s<>\"'，。！？「」『』（）【】]*)?"
    r")", re.IGNORECASE,
)


def _web_mask(text: str) -> bytearray:
    mask = bytearray(len(text))
    for match in _WEB_TOKEN.finditer(text):
        end = match.end()
        while end > match.start() and text[end - 1] in {"."} | _CLOSERS:
            end -= 1
        mask[match.start():end] = b"\x01" * (end - match.start())
    return mask


def _ambiguous_period(text: str, index: int) -> bool:
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    if before.isdigit() and after.isdigit():
        return True
    # Interior dots in names, domains, versions, and dotted abbreviations.
    if before.isascii() and after.isascii() and before.isalnum() and after.isalnum():
        return True
    prefix = text[max(0, index - 32):index + 1]
    if _DOTTED_ABBREVIATION.search(prefix):
        return True
    word = _WORD_BEFORE_DOT.search(prefix)
    if word:
        token = word.group(1)
        if token.lower() in _ABBREVIATIONS or (len(token) == 1 and token.isupper()):
            return True
    return False


def _sentence_ends(text: str) -> Iterator[int]:
    protected = _web_mask(text)
    index = 0
    while index < len(text):
        if text[index] not in _PUNCTUATION or protected[index]:
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] in _PUNCTUATION and not protected[end]:
            end += 1
        marks = text[index:end]
        complete = any(mark in _STRONG_END for mark in marks)
        if not complete:
            # A lone full stop can finish a sentence; '..', '...', and '…'
            # cannot justify ending the current audio activity by themselves.
            complete = marks == "." and not _ambiguous_period(text, index)
        if not complete:
            index = end
            continue
        # Keep closing quotes/brackets and any outer sentence punctuation with
        # the same sentence: e.g. He asked ("Really?").
        while end < len(text) and text[end] in _CLOSERS | _PUNCTUATION and not protected[end]:
            end += 1
        yield end
        index = end


def split_sentences(text: str) -> list[str]:
    """Split final transcription, including its unfinished trailing fragment.

    Only whitespace outside each result is stripped. Repeated sentences and
    internal whitespace are preserved; no normalization or deduplication occurs.
    """
    sentences: list[str] = []
    start = 0
    for end in _sentence_ends(text):
        sentence = text[start:end].strip()
        if sentence:
            sentences.append(sentence)
        start = end
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def completed_sentence_prefix(text: str) -> str:
    """Return the exact prefix through the first complete sentence, or "".

    Leading whitespace is retained so the result remains a literal prefix of
    the supplied interim hypothesis. Whitespace after the ending is excluded.
    """
    end = next(_sentence_ends(text), None)
    return text[:end] if end is not None else ""
