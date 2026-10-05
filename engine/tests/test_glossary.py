"""Bounded glossary reads and exact term selection; fixtures use temp files."""
import json
from pathlib import Path

import pytest

from engine.glossary import load_glossary, matching_glossary


def write_glossary(tmp_path, entries):
    path = tmp_path / "translation-glossary.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_file_means_empty_without_creation(tmp_path):
    path = tmp_path / "missing.json"
    assert load_glossary(path) == ([], None)
    assert not path.exists()


def test_unicode_glossary_loads_only_explicit_fields_and_normalizes_spaces(tmp_path):
    path = write_glossary(tmp_path, [
        {"source": " 师娘 ", "target": " 사모님 ", "note": "not sent to model"},
        {"source": "スバル", "target": "스바루"},
    ])
    before = path.read_bytes()
    assert load_glossary(path) == ([
        {"source": "师娘", "target": "사모님"},
        {"source": "スバル", "target": "스바루"},
    ], None)
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", [b"not json", b"[", b"\xff", b'{}', b'"text"'],
                         ids=["invalid", "unfinished", "non_utf8", "object", "string"])
def test_invalid_file_is_ignored_with_warning_and_kept_unchanged(tmp_path, raw):
    path = tmp_path / "glossary.json"
    path.write_bytes(raw)
    entries, warning = load_glossary(path)
    assert entries == [] and warning
    assert path.read_bytes() == raw


def test_bom_from_windows_editor_is_accepted(tmp_path):
    path = tmp_path / "glossary.json"
    path.write_text('[{"source":"韩立","target":"한립"}]', encoding="utf-8-sig")
    assert load_glossary(path) == ([{"source": "韩立", "target": "한립"}], None)


def test_oversize_file_is_ignored_with_warning(tmp_path):
    path = tmp_path / "glossary.json"
    path.write_bytes(b" " * (64 * 1024 + 1))
    entries, warning = load_glossary(path)
    assert entries == [] and "64 KiB" in warning


def test_read_failure_is_nonfatal_and_does_not_expose_exception(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("sensitive exception details")
    monkeypatch.setattr(Path, "open", denied)
    entries, warning = load_glossary(tmp_path / "glossary.json")
    assert entries == [] and warning
    assert "sensitive" not in warning


def test_invalid_entries_and_duplicates_are_skipped_while_valid_terms_survive(tmp_path):
    entries = [
        {"source": "师娘", "target": "사모님"},
        {"source": "师娘", "target": "different duplicate"},
        {"source": "", "target": "blank"},
        {"source": "X" * 65, "target": "long source"},
        {"source": "valid", "target": "X" * 65},
        {"source": "bad\nline", "target": "newline"},
        {"source": "bad\u0000value", "target": "control"},
        {"source": "unknown", "target": None},
        "not an entry",
        {"source": "韩立", "target": "한립"},
    ]
    loaded, warning = load_glossary(write_glossary(tmp_path, entries))
    assert loaded == [{"source": "师娘", "target": "사모님"}, {"source": "韩立", "target": "한립"}]
    assert warning


def test_catalog_is_bounded_to_200_entries(tmp_path):
    loaded, warning = load_glossary(write_glossary(tmp_path, [
        {"source": f"term{i}", "target": f"표기{i}"} for i in range(201)
    ]))
    assert len(loaded) == 200 and warning


def test_only_current_source_terms_are_selected():
    entries = [{"source": "师娘", "target": "사모님"}, {"source": "韩立", "target": "한립"}]
    assert matching_glossary("韩立向大家问好。", entries) == [entries[1]]
    assert matching_glossary("Hello.", entries) == []


def test_ascii_terms_match_words_without_matching_substrings():
    entries = [{"source": "AI", "target": "에이아이"}]
    assert matching_glossary("SAID", entries) == []
    assert matching_glossary("AI can help.", entries) == entries
    assert matching_glossary("AI로 도와요.", entries) == entries
    assert matching_glossary("ai", entries) == []


def test_prompt_terms_are_limited_to_20_and_longer_terms_have_priority():
    entries = [{"source": f"term{i}", "target": f"표기{i}"} for i in range(22)]
    result = matching_glossary(" ".join(item["source"] for item in entries), entries)
    assert len(result) == 20
    assert [len(item["source"]) for item in result] == sorted(
        [len(item["source"]) for item in result], reverse=True)


def test_direct_caller_cannot_bypass_entry_bounds():
    entries = [{"source": "name", "target": "X" * 65}, {"source": "name", "target": "이름"}]
    assert matching_glossary("name", entries) == [entries[1]]
    assert matching_glossary("name", {"name": "이름"}) == []
