"""ASR options persist without modifying model/language/key selections."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from engine.server import create_app
from engine.settings import DEFAULTS, EngineError, Settings
from engine.tests.test_engine import FakeRuntime, start


def test_legacy_settings_read_new_defaults_without_rewriting_file(tmp_path):
    legacy = {"mode": "gemini", "language": "ja", "asr_model": "models/asr/custom",
              "translation_model": "models/translation/custom.gguf"}
    path = tmp_path / "config/settings.json"
    path.parent.mkdir()
    encoded = json.dumps(legacy).encode()
    path.write_bytes(encoded)
    settings = Settings(tmp_path)
    assert settings.data == {**legacy, "asr_profile": "legacy", "asr_hints": [], "qwen_boundary_recheck": False,
                             "target_language": "ko"}
    assert path.read_bytes() == encoded


def test_mutable_default_and_caller_list_are_not_shared(tmp_path):
    one, two = Settings(tmp_path / "one"), Settings(tmp_path / "two")
    one.data["asr_hints"].append("first")
    assert two.data["asr_hints"] == DEFAULTS["asr_hints"] == []
    supplied = ["name"]
    one.update({"asr_hints": supplied})
    supplied.append("later")
    assert one.data["asr_hints"] == ["name"]


def test_round_trip_normalization_omission_and_explicit_clear(tmp_path):
    settings = Settings(tmp_path)
    settings.update({"asr_hints": ["  顧小糖  ", "", "cafe\u0301", "café", "陸擎淵"],
                     "qwen_boundary_recheck": False, "language": "zh"})
    expected = ["顧小糖", "café", "陸擎淵"]
    assert settings.public()["asr_hints"] == expected
    restored = Settings(tmp_path)
    assert restored.data["asr_hints"] == expected
    assert restored.data["qwen_boundary_recheck"] is False
    restored.update({"mode": "gemini"})
    assert restored.data["asr_hints"] == expected
    assert restored.data["qwen_boundary_recheck"] is False
    restored.update({"asr_hints": [], "qwen_boundary_recheck": True})
    assert Settings(tmp_path).data["asr_hints"] == []
    assert Settings(tmp_path).data["qwen_boundary_recheck"] is True
    assert Settings(tmp_path).data["language"] == "zh"


@pytest.mark.parametrize("bad", [None, "term", {}, True, [1], ["x"] * 33,
                                  ["x" * 49], ["word\ncontrol"], ["<|im_start|>"],
                                  [str(i) + "x" * 46 for i in range(12)]])
def test_invalid_hint_patch_is_atomic_and_cannot_change_key(tmp_path, bad):
    settings = Settings(tmp_path)
    settings.update({"asr_hints": ["saved"], "gemini_api_key": "test-original-secret", "language": "ja"})
    before = settings.file.read_bytes()
    with pytest.raises(EngineError) as error:
        settings.update({"asr_hints": bad, "language": "zh", "gemini_api_key": "test-new-secret"})
    assert error.value.code == "invalid_asr_hints" and error.value.status == 400
    assert settings.file.read_bytes() == before
    assert settings.key() == "test-original-secret"
    assert settings.data["language"] == "ja"


@pytest.mark.parametrize("bad", [None, 0, 1, "true", "false", [], {}])
def test_boundary_setting_requires_boolean(tmp_path, bad):
    settings = Settings(tmp_path)
    with pytest.raises(EngineError) as error:
        settings.update({"qwen_boundary_recheck": bad})
    assert error.value.status == 400
    assert settings.data["qwen_boundary_recheck"] is False
    assert not settings.file.exists()


def test_api_partial_options_preserve_other_settings_and_key(tmp_path):
    app = create_app(tmp_path, FakeRuntime())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + app.state.settings.token}
        initial = client.put("/v1/settings", headers=headers, json={
            "mode": "gemini", "language": "ja", "asr_model": "models/asr/custom",
            "translation_model": "models/translation/custom.gguf", "gemini_api_key": "test-hidden-key"})
        assert initial.status_code == 200
        response = client.put("/v1/settings", headers=headers, json={
            "asr_hints": ["  Tokyo  ", "Tokyo"], "qwen_boundary_recheck": False})
        assert response.status_code == 200
        current = client.get("/v1/settings", headers=headers).json()
        assert current["asr_hints"] == ["Tokyo"] and current["qwen_boundary_recheck"] is False
        for name in ("mode", "language", "asr_model", "translation_model", "gemini_key_set", "save_gemini_key"):
            assert current[name] == initial.json()[name]
        assert app.state.settings.key() == "test-hidden-key"
        assert "test-hidden-key" not in response.text
        assert "test-hidden-key" not in app.state.settings.file.read_text()
        failed = client.put("/v1/settings", headers=headers, json={"asr_hints": None})
        assert failed.status_code == 400
        assert client.get("/v1/settings", headers=headers).json()["asr_hints"] == ["Tokyo"]


def test_prepare_receives_persisted_options_and_active_session_rejects_changes(tmp_path):
    class ObservedRuntime(FakeRuntime):
        async def prepare(self, settings):
            self.prepared_settings = deepcopy(settings)
            await super().prepare(settings)

    runtime = ObservedRuntime()
    app = create_app(tmp_path, runtime)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + app.state.settings.token}
        patch = {"asr_hints": ["Game name"], "qwen_boundary_recheck": False}
        assert client.put("/v1/settings", headers=headers, json=patch).status_code == 200
        assert client.post("/v1/prepare", headers=headers, json={}).status_code == 200
        for name, value in patch.items():
            assert runtime.prepared_settings[name] == value
        with client.websocket_connect("/v1/stream") as ws:
            start(ws, app.state.settings.token)
            assert client.put("/v1/settings", headers=headers, json={"asr_hints": []}).status_code == 409
            assert client.put("/v1/settings", headers=headers, json={"qwen_boundary_recheck": True}).status_code == 409
            assert all(app.state.settings.data[name] == value for name, value in patch.items())
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "stopped"


@pytest.mark.parametrize("supplied,expected", [("legacy", "legacy"), (" STABLE ", "stable"), ("AlignAtt", "alignatt")])
def test_asr_profile_normalizes_persists_and_omission_preserves_selection(tmp_path, supplied, expected):
    settings = Settings(tmp_path)
    assert settings.public()["asr_profile"] == "legacy"
    settings.update({"asr_profile": supplied, "language": "ja", "asr_hints": ["title"]})
    settings.update({"mode": "gemini"})
    restored = Settings(tmp_path)
    assert restored.public()["asr_profile"] == expected
    assert restored.data["language"] == "ja" and restored.data["asr_hints"] == ["title"]
    assert restored.data["qwen_boundary_recheck"] is False


@pytest.mark.parametrize("bad", [None, "", "unknown", 1, True, [], {}])
def test_invalid_profile_cannot_modify_other_settings_or_key(tmp_path, bad):
    settings = Settings(tmp_path)
    settings.update({"asr_profile": "legacy", "language": "ja", "gemini_api_key": "test-profile-key"})
    before = settings.file.read_bytes()
    with pytest.raises(EngineError) as error:
        settings.update({"asr_profile": bad, "language": "zh", "gemini_api_key": "test-replacement"})
    assert error.value.code == "invalid_asr_profile" and error.value.status == 400
    assert settings.file.read_bytes() == before
    assert settings.key() == "test-profile-key"


def test_api_profile_roundtrip_prepare_and_active_session_guard(tmp_path):
    class ObservedRuntime(FakeRuntime):
        async def prepare(self, settings):
            self.prepared_settings = deepcopy(settings)
            await super().prepare(settings)

    runtime = ObservedRuntime()
    app = create_app(tmp_path, runtime)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + app.state.settings.token}
        original = client.get("/v1/settings", headers=headers).json()
        assert original["asr_profile"] == "legacy"
        response = client.put("/v1/settings", headers=headers, json={"asr_profile": " STABLE "})
        assert response.status_code == 200 and response.json()["asr_profile"] == "stable"
        for field in ("mode", "language", "asr_model", "translation_model", "asr_hints", "qwen_boundary_recheck"):
            assert response.json()[field] == original[field]
        assert client.post("/v1/prepare", headers=headers, json={}).status_code == 200
        assert runtime.prepared_settings["asr_profile"] == "stable"
        rejected = client.put("/v1/settings", headers=headers, json={"asr_profile": "bad"})
        assert rejected.status_code == 400
        assert client.get("/v1/settings", headers=headers).json()["asr_profile"] == "stable"
        with client.websocket_connect("/v1/stream") as ws:
            start(ws, app.state.settings.token)
            assert client.put("/v1/settings", headers=headers, json={"asr_profile": "legacy"}).status_code == 409
            assert app.state.settings.data["asr_profile"] == "stable"
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "stopped"
