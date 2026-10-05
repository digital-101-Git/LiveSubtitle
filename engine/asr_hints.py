"""Bounded, user-supplied source vocabulary for Qwen recognition only.

No transcript, subtitle reference, translation glossary or history is loaded here.
Validation is independent of settings so both settings and runtime can reuse it.
"""
from __future__ import annotations

import re
import unicodedata


MAX_ASR_HINTS = 32
MAX_ASR_HINT_CHARACTERS = 48
MAX_ASR_HINT_TOTAL_CHARACTERS = 512
_MODEL_CONTROL = re.compile(
    r"<\||\|>|\[/?INST\]|<</?SYS>>|</?(?:asr_text|think|start_of_turn|end_of_turn)>",
    re.IGNORECASE,
)


def validate_asr_hints(value) -> list[str]:
    """Return a fresh normalized list or raise ValueError without echoing input.

    Limits apply to incoming list length and normalized retained term lengths.
    Blank terms are omitted; exact duplicate terms keep their first position.
    Similar spellings, case variants and homophones remain distinct.
    """
    if not isinstance(value, list) or len(value) > MAX_ASR_HINTS:
        raise ValueError("ASR hints must be a list with at most 32 entries.")
    result, seen = [], set()
    characters = 0
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Each ASR hint must be text.")
        term = unicodedata.normalize("NFC", item).strip()
        if not term:
            continue
        if (len(term) > MAX_ASR_HINT_CHARACTERS
                or any(unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in term)
                or _MODEL_CONTROL.search(term)):
            raise ValueError("ASR hints must be short source terms without controls or model tokens.")
        if term in seen:
            continue
        characters += len(term)
        if characters > MAX_ASR_HINT_TOTAL_CHARACTERS:
            raise ValueError("ASR hints exceed 512 total characters.")
        result.append(term)
        seen.add(term)
    return result


def build_asr_hint_prompt(hints: list[str]) -> str:
    """Use the processor's supported context/hotword prompt, empty by default."""
    terms = validate_asr_hints(hints)
    return "Vocabulary: " + ", ".join(terms) if terms else ""
