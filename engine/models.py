"""Inventory and explicit file import. Never downloads model weights."""
from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

from .settings import EngineError, validate_gguf

ASR_FILES = ("model.bin", "config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json")
QWEN_ASR_FILES = ("config.json", "processor_config.json", "chat_template.jinja",
                  "tokenizer.json", "tokenizer_config.json", "generation_config.json")


def _model_file(directory: Path, name: str) -> Path:
    # Safetensors indexes and file links may not escape the selected model.
    if not isinstance(name, str) or not name or Path(name).name != name or ":" in name or "\\" in name:
        raise ValueError("invalid model filename")
    path = directory / name
    if not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("missing or unconfined model file")
    return path


def _read_metadata(directory: Path, name: str) -> dict:
    path = _model_file(directory, name)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("metadata too large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    return value


def _safetensors_complete(path: Path) -> set[str]:
    """Check header offsets against on-disk size without reading GBs of weights."""
    with path.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise ValueError("incomplete safetensors header")
        length = struct.unpack("<Q", prefix)[0]
        if not 2 <= length <= 16 * 1024 * 1024 or length + 8 > path.stat().st_size:
            raise ValueError("invalid safetensors header size")
        header = json.loads(handle.read(length))
    if not isinstance(header, dict):
        raise ValueError("invalid safetensors header")
    spans = []
    names = set()
    for name, tensor in header.items():
        if name == "__metadata__":
            continue
        if not isinstance(tensor, dict):
            raise ValueError("invalid tensor metadata")
        offsets = tensor.get("data_offsets")
        if (not isinstance(offsets, list) or len(offsets) != 2
                or any(type(value) is not int for value in offsets)
                or not 0 <= offsets[0] <= offsets[1]):
            raise ValueError("invalid tensor offsets")
        names.add(name)
        spans.append(tuple(offsets))
    end = 0
    for start, stop in sorted(spans):
        if start != end:
            raise ValueError("non-contiguous tensor data")
        end = stop
    if not names or end != path.stat().st_size - length - 8:
        raise ValueError("incomplete safetensors data")
    return names


def asr_backend(path: Path) -> str:
    """Validate a complete local model and identify its native inference backend."""
    try:
        if not path.is_dir():
            raise ValueError("not a model directory")
        config = _read_metadata(path, "config.json")
        if config.get("model_type") == "qwen3_asr":
            if "Qwen3ASRForConditionalGeneration" not in config.get("architectures", []):
                raise ValueError("unsupported Qwen ASR architecture")
            for name in QWEN_ASR_FILES:
                _model_file(path, name)
            index_path = path / "model.safetensors.index.json"
            if index_path.exists():
                index = _read_metadata(path, index_path.name).get("weight_map")
                if not isinstance(index, dict) or not index or not all(isinstance(k, str) for k in index):
                    raise ValueError("invalid safetensors index")
                shards = {}
                for filename in set(index.values()):
                    if not isinstance(filename, str) or not filename.endswith(".safetensors"):
                        raise ValueError("invalid safetensors shard")
                    shards[filename] = _safetensors_complete(_model_file(path, filename))
                if any(name not in shards[filename] for name, filename in index.items()):
                    raise ValueError("indexed tensor missing")
            else:
                _safetensors_complete(_model_file(path, "model.safetensors"))
            return "qwen3_asr"
        if config.get("model_type") not in (None, "whisper"):
            raise ValueError("unsupported model type")
        for name in ASR_FILES:
            _model_file(path, name)
        return "whisper"
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise EngineError("asr_incomplete", "음성 인식 모델 파일이 불완전하거나 지원하지 않는 형식입니다. 모델 설치를 확인해 주세요.", 409) from exc


def inventory(root: Path) -> dict:
    result = {"translation": [], "asr": []}
    for kind in result:
        base = root / "models" / kind
        if not base.resolve().is_relative_to(root.resolve()):
            continue
        base.mkdir(parents=True, exist_ok=True)
        if kind == "translation":
            for path in sorted(base.glob("*.gguf")):
                if path.is_file() and path.resolve().is_relative_to(base.resolve()):
                    result[kind].append({"name": path.name, "path": path.relative_to(root).as_posix(),
                                         "size": path.stat().st_size})
        else:
            for path in sorted(base.iterdir()):
                if not path.is_dir() or not path.resolve().is_relative_to(base.resolve()):
                    continue
                try:
                    backend = asr_backend(path)
                except EngineError:
                    continue
                result[kind].append({"name": path.name, "path": path.relative_to(root).as_posix(),
                                     "backend": backend})
    return result


def import_gguf(root: Path, source: str) -> dict:
    raw = Path(source)
    if not raw.is_absolute():
        raise EngineError("invalid_model_path", "가져올 파일의 절대 경로가 필요합니다.")
    path = raw.resolve(strict=True)
    validate_gguf(path)
    target_dir = (root / "models" / "translation")
    target_dir.mkdir(parents=True, exist_ok=True)
    if not target_dir.resolve().is_relative_to(root.resolve()):
        raise EngineError("invalid_model_path", "모델 폴더가 앱 폴더 밖으로 연결되어 있습니다.")
    target = target_dir / path.name
    if target.exists():
        raise EngineError("model_exists", "같은 이름의 모델이 이미 있습니다. 덮어쓰지 않았습니다.", 409)
    # Exclusive target creation and cleanup cover concurrent imports and full disks.
    created = False
    try:
        with path.open("rb") as reader, target.open("xb") as writer:
            created = True
            shutil.copyfileobj(reader, writer, length=4 * 1024 * 1024)
        validate_gguf(target)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return {"name": target.name, "path": target.relative_to(root).as_posix(), "size": target.stat().st_size}
