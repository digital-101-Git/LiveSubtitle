"""Read-only, bounded user glossary. Invalid files never stop live subtitles."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


MAX_GLOSSARY_BYTES = 64 * 1024
MAX_GLOSSARY_ENTRIES = 200
MAX_TERM_CHARS = 64
MAX_PROMPT_TERMS = 20


def _entry(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for field in ("source", "target"):
        text = value.get(field)
        if not isinstance(text, str):
            return None
        text = text.strip()
        if not text or len(text) > MAX_TERM_CHARS:
            return None
        if any(unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
               for character in text):
            return None
        result[field] = text
    return result


def load_glossary(path: Path) -> tuple[list[dict[str, str]], str | None]:
    """Return normalized entries plus an optional nonfatal Korean warning.

    Only the given file is read. No file is created, repaired, renamed, or logged.
    Missing files mean an empty glossary. Bad JSON, oversized data, or unreadable
    files mean an empty glossary and warning. Invalid/duplicate list entries are
    skipped while valid entries remain available; the first duplicate wins.
    """
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_GLOSSARY_BYTES + 1)
    except FileNotFoundError:
        return [], None
    except OSError:
        return [], "번역 용어집을 읽지 못했습니다. 용어집 없이 번역을 계속합니다."
    if len(raw) > MAX_GLOSSARY_BYTES:
        return [], "번역 용어집이 64 KiB를 넘습니다. 용어집 없이 번역을 계속합니다."
    try:
        values = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return [], "번역 용어집의 UTF-8 또는 JSON 형식이 잘못되었습니다. 용어집 없이 번역을 계속합니다."
    if not isinstance(values, list):
        return [], "번역 용어집은 source와 target을 가진 JSON 목록이어야 합니다. 용어집 없이 번역을 계속합니다."
    entries = []
    seen = set()
    ignored = len(values) > MAX_GLOSSARY_ENTRIES
    for value in values[:MAX_GLOSSARY_ENTRIES]:
        entry = _entry(value)
        if entry is None or entry["source"] in seen:
            ignored = True
            continue
        seen.add(entry["source"])
        entries.append(entry)
    warning = (
        "번역 용어집의 잘못된 항목, 중복 항목 또는 200개를 넘는 항목을 무시했습니다. "
        "원문과 번역어는 각각 1~64자의 한 줄이어야 합니다."
        if ignored else None
    )
    return entries, warning


def matching_glossary(
    text: str, glossary: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Select at most 20 exact source matches, preferring longer terms.

    Chinese/Japanese terms can occur inside a phrase. For ASCII words, require
    ASCII word boundaries so a glossary entry ``AI`` cannot match ``SAID``.
    Case and Unicode spelling remain significant; no fuzzy ASR repair is done.
    This also revalidates entries for callers that bypass load_glossary().
    """
    if not isinstance(glossary, list):
        return []
    matches = []
    seen = set()
    for value in glossary[:MAX_GLOSSARY_ENTRIES]:
        entry = _entry(value)
        if entry is None or entry["source"] in seen:
            continue
        term = entry["source"]
        seen.add(term)
        pattern = re.escape(term)
        if term[0].isascii() and (term[0].isalnum() or term[0] == "_"):
            pattern = r"(?<![A-Za-z0-9_])" + pattern
        if term[-1].isascii() and (term[-1].isalnum() or term[-1] == "_"):
            pattern += r"(?![A-Za-z0-9_])"
        if re.search(pattern, text):
            matches.append(entry)
    # Stable tie order is the file's order. A full person name takes priority
    # over a shorter substring if both exist in a large user glossary.
    matches.sort(key=lambda item: -len(item["source"]))
    return matches[:MAX_PROMPT_TERMS]
