"""Pure text prompt profiles; no model loading, tokenization, or network access.

TranslateGemma's preferred instruction is reproduced from Google's technical
report, Figure 3 (https://arxiv.org/pdf/2601.09012). Its message contract is at
https://huggingface.co/google/translategemma-12b-it#usage. Gemma model weights and
associated materials are governed by https://ai.google.dev/gemma/terms.
The public runtime turn format is also documented by Ollama:
https://ollama.com/library/translategemma:12b/blobs/e0a42594d802.

The gated Google HF chat_template.jinja was not accessed. Text and turn layout
were also checked against the tokenizer.chat_template embedded in the selected
GGUF, matching the metadata at bullerwins/translategemma-12b-it-GGUF revision
d7d1d8cc4ff53d4bc883ef33eae3894f07833b63 (including the newline after the first
instruction paragraph). This is an independent single-text renderer, not a copy
of the full language table or multimodal/multi-turn implementation. ``auto``
resolution is an application heuristic, not a TranslateGemma template feature.
"""
from __future__ import annotations

import re
from pathlib import Path

from .glossary import matching_glossary
from .settings import normalize_target_language


_LANGUAGE_NAMES = {"en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean"}
_CHINESE_LANGUAGE_NAMES = {"en": "英语", "zh": "中文", "ja": "日语", "ko": "韩语"}
_MODEL_NAME = re.compile(r"(?:^|[^a-z0-9])translate[-_]?gemma(?:$|[^a-z])", re.IGNORECASE)
_HYMT2_MODEL_NAME = re.compile(r"(?:^|[^a-z0-9])hy[-_]?mt2(?:$|[^a-z0-9])", re.IGNORECASE)
_MILMMT_MODEL_NAME = re.compile(r"(?:^|[^a-z0-9])milmmt[-_]46(?:$|[^a-z0-9])", re.IGNORECASE)
_JAKOVN_MODEL_NAME = re.compile(r"(?:^|[^a-z0-9])ja[-_]ko[-_]vn[-_]12b[-_]v2(?:$|[^a-z0-9])", re.IGNORECASE)
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f\U0001b000-\U0001b16f]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7af\ud7b0-\ud7ff]")

# The raw completion endpoint parses special tokens in the complete prompt.
# Neutralize angle-bracket token spellings in *source data only*, including
# future vocabulary additions, so source text cannot create a turn or BOS/EOS.
# Ordinary comparison operators ("x < 3") and all other source text are kept.
_TOKEN_LITERAL = re.compile(r"<\|?[A-Za-z_][A-Za-z0-9_./:|+-]*\|?>")

TRANSLATEGEMMA_STOP = ("<end_of_turn>", "<eos>")
JAKOVN_STOP = ("<end_of_turn>", "<eos>")
HYMT2_CONTEXT_CHARS = 900


def is_jakovn_model(path: str | Path | None) -> bool:
    """Recognize the verified JA-KO-VN 12B v2 filename, not a parent folder."""
    if path is None:
        return False
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return _JAKOVN_MODEL_NAME.search(name) is not None


def build_jakovn_prompt(text: str, *, include_bos: bool = False) -> str:
    """Render the installed v2 GGUF's single-turn JA->KO translation template.

    https://huggingface.co/hell0ks/ja-ko-vn-12b-v2-gguf
    This translation-trained model preserves input structure, including JSON.
    Send only the current source in its user turn: no JSON envelope, previous
    captions, generic chat instructions or TranslateGemma instruction. The
    GGUF adds BOS itself; raw /completion callers must not add a second BOS.
    """
    if not isinstance(text, str):
        raise TypeError("Source text must be a string.")
    source = text.strip()
    if not source:
        raise ValueError("Source text must not be empty.")
    source = _TOKEN_LITERAL.sub(lambda match: "＜" + match[0][1:-1] + "＞", source)
    prefix = "<bos>" if include_bos else ""
    return (f"{prefix}<start_of_turn>system\n"
            "당신은 전문 일한 번역가입니다. 주어진 일본어를 한국어로 번역하세요."
            f"<end_of_turn>\n<start_of_turn>user\n{source}<end_of_turn>\n"
            "<start_of_turn>model\n")


def is_milmmt_model(path: str | Path | None) -> bool:
    """Recognize MiLMMT-46 GGUF filenames, never a parent folder alone."""
    if path is None:
        return False
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return _MILMMT_MODEL_NAME.search(name) is not None


def build_milmmt_prompt(
    text: str, source_language: str | None = "auto", *, target_language: str = "ko",
) -> str:
    """Render Xiaomi's plain completion prompt for a supported target language.

    https://huggingface.co/xiaomi-research/MiLMMT-46-12B-v1.0#translation-prompt
    The official tokenizer has add_bos_token=False and no chat template. Its
    inference examples use no special tokens and greedy decoding. Do not add
    Gemma chat turns, a system instruction, conversation context, or glossary.

    The app's ordinary zh source uses Chinese (Simplified). Explicit script or
    regional tags can select Chinese (Traditional); untagged Chinese is not
    guessed or rewritten. Other source detection follows the existing limited
    EN/ZH/JA/KO heuristic. Source token spellings are neutralized because the
    raw llama.cpp endpoint otherwise interprets them as special tokens.
    """
    target_code = normalize_target_language(target_language)
    if not isinstance(text, str):
        raise TypeError("Source text must be a string.")
    source = text.strip()
    if not source:
        raise ValueError("Source text must not be empty.")
    language = resolve_source_language(source, source_language)
    name = _LANGUAGE_NAMES[language]
    if language == "zh":
        variants = (source_language or "").strip().lower().replace("_", "-").split("-")[1:]
        traditional = "hant" in variants or ("hans" not in variants and any(
            region in variants for region in ("tw", "hk", "mo")))
        name = "Chinese (Traditional)" if traditional else "Chinese (Simplified)"
    source = _TOKEN_LITERAL.sub(lambda match: "＜" + match[0][1:-1] + "＞", source)
    target_name = "Chinese (Simplified)" if target_code == "zh" else _LANGUAGE_NAMES[target_code]
    return f"Translate this from {name} to {target_name}:\n{name}: {source}\n{target_name}:"


def is_translategemma_model(path: str | Path | None) -> bool:
    """Recognize a TranslateGemma GGUF filename, never a parent folder alone."""
    if path is None:
        return False
    # Handle Windows paths when unit tests or development run on another OS.
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return _MODEL_NAME.search(name) is not None


def resolve_source_language(text: str, language: str | None = "auto") -> str:
    """Resolve the app's EN/ZH/JA/KO sources into official base language codes.

    Explicit ASR/user language wins, including ``zh-Hant`` / ``en_US`` forms.
    Auto detection is deliberately limited to the app's four source languages:
    kana -> Japanese; Han -> Chinese; Hangul -> Korean; otherwise -> English.
    Thus Han-only Japanese is ambiguous and falls back to Chinese; choosing
    Japanese explicitly avoids this. Latin names, numbers, and symbols use en.
    Foreign script takes precedence over Hangul to avoid a mixed subtitle being
    incorrectly treated as already translated. This is not general language ID.
    """
    if not isinstance(text, str):
        raise TypeError("Source text must be a string.")
    if language is not None and not isinstance(language, str):
        raise TypeError("Source language must be a string or None.")
    code = (language or "auto").strip().lower().replace("_", "-") or "auto"
    if code != "auto":
        base = code.split("-", 1)[0]
        if base not in _LANGUAGE_NAMES:
            raise ValueError(f"Unsupported translation source language: {language!r}")
        return base
    if _KANA.search(text):
        return "ja"
    if _HAN.search(text):
        return "zh"
    if _HANGUL.search(text):
        return "ko"
    return "en"


def build_translategemma_prompt(
    text: str, source_language: str | None, *, include_bos: bool = False,
    target_language: str = "ko",
) -> str:
    """Render one official-style text translation turn for the selected target.

    For llama.cpp ``/completion``, leave ``include_bos=False`` when the model's
    tokenizer adds BOS automatically. Otherwise use True and disable automatic
    BOS insertion: exactly one BOS is required. No assistant end token is added
    because the model must generate that turn. Stop on TRANSLATEGEMMA_STOP.

    A caller may return same-language input unchanged instead of invoking the model.
    No context, system role, correction instructions, or JSON envelope is added:
    these are not part of TranslateGemma's trained text translation template.
    """
    target_code = normalize_target_language(target_language)
    target_name = _LANGUAGE_NAMES[target_code]
    if not isinstance(text, str):
        raise TypeError("Source text must be a string.")
    source = text.strip()
    if not source:
        raise ValueError("Source text must not be empty.")
    code = resolve_source_language(source, source_language)
    name = _LANGUAGE_NAMES[code]
    source = _TOKEN_LITERAL.sub(lambda match: "＜" + match[0][1:-1] + "＞", source)
    instruction = (
        f"You are a professional {name} ({code}) to {target_name} ({target_code}) translator. "
        "Your goal is to accurately convey the meaning and nuances of the "
        f"original {name} text while adhering to {target_name} grammar, vocabulary, "
        f"and cultural sensitivities.\nProduce only the {target_name} translation, "
        "without any additional explanations or commentary. Please translate "
        f"the following {name} text into {target_name}:\n\n\n{source}"
    )
    prefix = "<bos>" if include_bos else ""
    return f"{prefix}<start_of_turn>user\n{instruction}<end_of_turn>\n<start_of_turn>model\n"


def is_hymt2_model(path: str | Path | None) -> bool:
    """Recognize Hy-MT2 by filename without changing older model profiles."""
    if path is None:
        return False
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return _HYMT2_MODEL_NAME.search(name) is not None


def build_hymt2_messages(
    text: str, source_language: str | None = "auto", context: list[str] | None = None,
    glossary: list[dict[str, str]] | None = None,
    *, target_language: str = "ko",
) -> list[dict[str, str]]:
    """Build Tencent's user-only request for the selected target language.

    Instruction templates (Apache-2.0 repository/model):
    https://github.com/Tencent-Hunyuan/Hy-MT2#hy-mt2-translation-task-instruction-examples-chinese-english-comparison
    https://huggingface.co/tencent/Hy-MT2-7B-GGUF
    https://huggingface.co/tencent/Hy-MT2-7B/raw/main/chat_template.jinja

    The official chat template turns this one user message into
    ``<|startoftext|>{content}<|extra_0|>``. Let llama.cpp apply that embedded
    template; do not add BOS, assistant prefixes, or Qwen thinking switches here.
    The model has no default system prompt. Source language only selects the
    instruction's language, not an asserted source-language label: its default
    prompt accepts source text directly. English/Japanese use the English
    instruction; Chinese uses the corresponding official Chinese instruction.

    Context uses the official background-information format; callers supply
    only finalized previous source sentences. We keep at most the latest three
    entries within 900 characters, without cutting a sentence in half. Terms
    use the official terminology format and match the current source only.
    The concise subtitle-style instructions are application-specific additions
    based on Tencent's style/personalization examples, not a vendor guarantee.
    Keep the current source unlabelled after the final instruction: a source
    heading was copied into real outputs, causing Korean-only validation to fail.
    """
    target_code = normalize_target_language(target_language)
    if not isinstance(text, str):
        raise TypeError("Source text must be a string.")
    source = text.strip()
    if not source:
        raise ValueError("Source text must not be empty.")
    language = resolve_source_language(source, source_language)
    # The installed glossary contains Korean translations, not multilingual
    # terminology. Never inject its Korean targets into another output language.
    terms = matching_glossary(source, glossary) if target_code == "ko" else []
    source = _TOKEN_LITERAL.sub(lambda match: "＜" + match[0][1:-1] + "＞", source)
    if context is not None and not isinstance(context, list):
        raise TypeError("Context must be a list of strings or None.")
    previous: list[str] = []
    context_chars = 0
    # Prefer recent complete sentences when the total context exceeds the cap.
    for entry in reversed((context or [])[-3:]):
        if not isinstance(entry, str):
            raise TypeError("Context entries must be strings.")
        entry = entry.strip()
        cost = len(entry) + bool(previous)
        if entry and context_chars + cost <= HYMT2_CONTEXT_CHARS:
            previous.append(_TOKEN_LITERAL.sub(
                lambda match: "＜" + match[0][1:-1] + "＞", entry))
            context_chars += cost
    previous.reverse()
    sections = []
    if previous:
        background = "\n".join(previous)
        heading = "〖背景信息〗" if language == "zh" else "[Background Information]"
        sections.append(f"{heading}\n{background}")
    if terms:
        lines = []
        for term in terms:
            original = _TOKEN_LITERAL.sub(lambda match: "＜" + match[0][1:-1] + "＞", term["source"])
            target = _TOKEN_LITERAL.sub(lambda match: "＜" + match[0][1:-1] + "＞", term["target"])
            lines.append(f"{original} 翻译成 {target}" if language == "zh"
                         else f"{original} translates to {target}")
        heading = "参考下面的翻译：" if language == "zh" else "Reference the following translations:"
        sections.append(heading + "\n" + "\n".join(lines))
    if language == "zh":
        target_name = _CHINESE_LANGUAGE_NAMES[target_code]
        sections.append(
            f"使用自然、简洁的{target_name}字幕，保留原文的敬语、非敬语和语气。"
            "不要添加原文没有的辱骂、事实或推测，不要补全未说完的话。"
            "仅翻译下面的原文，背景信息只供理解，不要翻译背景信息。\n"
            f"将以下文本翻译为{target_name}，注意只需要输出翻译后的结果，不要额外解释：\n\n"
            f"{source}"
        )
    else:
        target_name = _LANGUAGE_NAMES[target_code]
        sections.append(
            f"Use natural, concise {target_name} subtitles. Preserve the source's politeness, "
            "formality, and tone. Do not add insults, facts, guesses, or missing sentence endings. "
            "Translate only the following source text; use background information only for understanding, "
            "and do not translate it.\n"
            f"Translate the following text into {target_name}. Note that you should only "
            "output the translated result without any additional explanation:\n\n"
            f"{source}"
        )
    return [{"role": "user", "content": "\n\n".join(sections)}]
