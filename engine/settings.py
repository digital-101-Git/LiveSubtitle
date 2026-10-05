"""Validated on-disk configuration and Windows CurrentUser secret storage."""
from __future__ import annotations

import ctypes
from copy import deepcopy
import json
import os
import secrets
from pathlib import Path

from .asr_hints import validate_asr_hints


class EngineError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def atomic_write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    try:
        if isinstance(value, bytes):
            tmp.write_bytes(value)
        else:
            tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def dpapi(value: bytes, encrypt: bool) -> bytes:
    if os.name != "nt":
        raise EngineError("secret_storage", "API 키 보관은 Windows DPAPI 환경에서 지원됩니다.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN; omit LOCAL_MACHINE => current Windows user only.
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise EngineError("secret_storage", "Windows API 키 암호화/복호화에 실패했습니다.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


DEFAULTS = {
    "mode": "local",
    "language": "auto",
    "target_language": "ko",
    "translation_model": "models/translation/Qwen3.5-4B-Q4_K_M.gguf",
    "asr_model": "models/asr/whisper-large-v3-turbo",
    "asr_profile": "legacy",
    "asr_hints": [],
    "qwen_boundary_recheck": False,
}


def normalize_language(value: str | None) -> str:
    """An explicitly empty selection means auto; callers handle omitted fields."""
    if value is None:
        return "auto"
    if not isinstance(value, str):
        raise EngineError("invalid_language", "입력 언어는 auto, ko, en, zh, ja입니다.")
    language = value.strip().lower()
    if language in ("", "auto"):
        return "auto"
    if language not in ("ko", "en", "zh", "ja"):
        raise EngineError("invalid_language", "입력 언어는 auto, ko, en, zh, ja입니다.")
    return language


def normalize_target_language(value: str | None) -> str:
    """Empty output selection defaults to Korean; output never auto-detects."""
    if value is None:
        return "ko"
    if not isinstance(value, str):
        raise EngineError("invalid_target_language", "번역 언어는 ko, en, zh, ja입니다.")
    language = value.strip().lower() or "ko"
    if language not in ("ko", "en", "zh", "ja"):
        raise EngineError("invalid_target_language", "번역 언어는 ko, en, zh, ja입니다.")
    return language


def normalize_asr_profile(value: str) -> str:
    if not isinstance(value, str) or value.strip().lower() not in ("legacy", "stable", "alignatt"):
        raise EngineError("invalid_asr_profile", "음성 확정 방식은 legacy, stable, alignatt 중 하나여야 합니다.")
    return value.strip().lower()


class Settings:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = self.root / "config"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = self.directory / "settings.json"
        self.key_file = self.directory / "gemini-key.dpapi"
        self.ephemeral_key: str | None = None
        token_file = self.directory / "engine-token.txt"
        if token_file.exists():
            self.token = token_file.read_text(encoding="utf-8-sig").strip()
            if len(self.token) < 32:
                raise EngineError("invalid_token_file", "엔진 토큰 파일이 손상되었습니다.")
        else:
            self.token = secrets.token_urlsafe(32)
            # Exclusive creation prevents two engine starts from replacing tokens.
            try:
                with token_file.open("x", encoding="utf-8") as handle:
                    handle.write(self.token)
            except FileExistsError:
                self.token = token_file.read_text(encoding="utf-8-sig").strip()
        self.data = deepcopy(DEFAULTS)
        if self.file.exists():
            try:
                loaded = json.loads(self.file.read_text(encoding="utf-8-sig"))
                self._validate(loaded)
                self.data.update(loaded)
            except (ValueError, TypeError) as exc:
                raise EngineError("invalid_settings", "설정 파일을 읽을 수 없습니다.") from exc

    def _validate(self, patch: dict) -> None:
        if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
            raise EngineError("invalid_settings", "지원하지 않는 설정 항목입니다.")
        if "mode" in patch and patch["mode"] not in ("local", "gemini"):
            raise EngineError("invalid_mode", "음성 인식 모드는 local 또는 gemini여야 합니다.")
        if "language" in patch:
            patch["language"] = normalize_language(patch["language"])
        if "target_language" in patch:
            patch["target_language"] = normalize_target_language(patch["target_language"])
        if "asr_profile" in patch:
            patch["asr_profile"] = normalize_asr_profile(patch["asr_profile"])
        if "asr_hints" in patch:
            try:
                patch["asr_hints"] = validate_asr_hints(patch["asr_hints"])
            except ValueError as exc:
                raise EngineError("invalid_asr_hints", "음성 인식 힌트는 최대 32개, 항목당 48자, 합계 512자여야 합니다. 제어문자와 모델 특수 토큰은 사용할 수 없습니다.") from exc
        if "qwen_boundary_recheck" in patch and not isinstance(patch["qwen_boundary_recheck"], bool):
            raise EngineError("invalid_settings", "문장 경계 재확인 옵션은 참/거짓 값이어야 합니다.")
        for key, folder in (("translation_model", "translation"), ("asr_model", "asr")):
            if key in patch:
                confined_model(self.root, patch[key], folder, must_exist=False)

    def update(self, patch: dict) -> None:
        patch = dict(patch)
        key = patch.pop("gemini_api_key", None)
        save_key = patch.pop("save_gemini_key", False)
        if not isinstance(save_key, bool):
            raise EngineError("invalid_settings", "키 저장 옵션은 참/거짓 값이어야 합니다.")
        self._validate(patch)
        if key is not None and (not isinstance(key, str) or len(key) > 1024):
            raise EngineError("invalid_key", "API 키 형식이 올바르지 않습니다.")
        # Encrypt before changing ordinary settings, so failure cannot claim success.
        if key is not None:
            if key.strip():
                if save_key:
                    atomic_write(self.key_file, dpapi(key.strip().encode(), True))
                    self.ephemeral_key = None
                else:
                    self.ephemeral_key = key.strip()
                    self.key_file.unlink(missing_ok=True)
            else:
                self.ephemeral_key = None
                self.key_file.unlink(missing_ok=True)
        merged = {**self.data, **patch}
        atomic_write(self.file, json.dumps(merged, ensure_ascii=False, indent=2))
        self.data = merged

    def key(self) -> str:
        if self.ephemeral_key:
            return self.ephemeral_key
        if not self.key_file.exists():
            raise EngineError("missing_api_key", "Gemini API 키를 먼저 입력해 주세요.")
        try:
            return dpapi(self.key_file.read_bytes(), False).decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise EngineError("secret_storage", "저장된 API 키를 읽을 수 없습니다.") from exc

    def public(self) -> dict:
        return {**self.data, "gemini_key_set": bool(self.ephemeral_key) or self.key_file.exists(),
                "save_gemini_key": self.key_file.exists()}


def confined_model(root: Path, relative: str, kind: str, must_exist: bool = True) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise EngineError("invalid_model_path", "모델은 앱 models 폴더 안의 상대 경로여야 합니다.")
    base = (root / "models" / kind).resolve()
    candidate = (root / relative).resolve()
    if not base.is_relative_to(root.resolve()) or not candidate.is_relative_to(base) or candidate == base:
        raise EngineError("invalid_model_path", "허용된 모델 폴더 밖의 경로입니다.")
    if must_exist and not candidate.exists():
        raise EngineError("model_missing", "선택한 모델 파일을 찾을 수 없습니다.", 409)
    return candidate


def validate_gguf(path: Path) -> None:
    if path.suffix.lower() != ".gguf" or not path.is_file():
        raise EngineError("invalid_model", "GGUF 모델 파일을 선택해 주세요.")
    with path.open("rb") as handle:
        if handle.read(4) != b"GGUF":
            raise EngineError("invalid_model", "올바른 GGUF 모델 파일이 아닙니다.")
