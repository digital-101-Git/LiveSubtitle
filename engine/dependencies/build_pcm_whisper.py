"""Build the pinned faster-whisper PCM fork using only Python's standard library.

This changes audio.py, version.py, and wheel metadata only. VAD and its asset are
copied byte-for-byte from the verified upstream wheel. No dependency is imported.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile


VERSION = "1.2.1+livesubtitle.pcm1"
UPSTREAM_FILENAME = "faster_whisper-1.2.1-py3-none-any.whl"
UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/05/99/"
    "49ee85903dee060d9f08297b4a342e5e0bcfca2f027a07b4ee0a38ab13f9/"
    + UPSTREAM_FILENAME
)
UPSTREAM_SHA256 = "79a66ad50688c0b794dd501dc340a736992a6342f7f95e5811be60b5224a26a7"
AUDIO_ORIGINAL_SHA256 = "60a1d8638f718cbf6d245aed3e5a5aa61c1f822a0b0fe9b48a7c928d47c23909"
AUDIO_PCM_SHA256 = "e47cb57c67cdb61a4910db03fffec965223d41ca48982d7238db892c1a470016"
VAD_SHA256 = "37a9c774aefdd3162d936b896c8dcf5571b2ed938d65bffecfd631770049a18d"
VAD_ASSET_SHA256 = "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2"
OLD_INFO = "faster_whisper-1.2.1.dist-info"
NEW_INFO = f"faster_whisper-{VERSION}.dist-info"
WHEEL_FILENAME = f"faster_whisper-{VERSION}-py3-none-any.whl"
SOURCE_DIR = Path(__file__).resolve().parent / "faster_whisper_pcm"
APP_ROOT = Path(__file__).resolve().parents[2]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked(data: bytes, digest: str, description: str) -> bytes:
    if sha256(data) != digest:
        raise ValueError(f"Unexpected SHA256 for {description}; refusing to patch.")
    return data


def upstream_files(wheel_path: Path) -> dict[str, bytes]:
    raw = checked(wheel_path.read_bytes(), UPSTREAM_SHA256, "upstream wheel")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def make_record(files: dict[str, bytes], info: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, data in sorted(files.items()):
        if name == f"{info}/RECORD":
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + digest.decode("ascii"), str(len(data))))
    writer.writerow((f"{info}/RECORD", "", ""))
    return output.getvalue().encode("utf-8")


def patched_files(original: dict[str, bytes]) -> dict[str, bytes]:
    checked(original["faster_whisper/audio.py"], AUDIO_ORIGINAL_SHA256, "upstream audio.py")
    checked(original["faster_whisper/vad.py"], VAD_SHA256, "upstream vad.py")
    checked(original["faster_whisper/assets/silero_vad_v6.onnx"], VAD_ASSET_SHA256, "Silero v6 asset")
    files = {
        name.replace(OLD_INFO + "/", NEW_INFO + "/", 1): data
        for name, data in original.items()
        if name != f"{OLD_INFO}/RECORD"
    }
    files["faster_whisper/audio.py"] = checked(
        (SOURCE_DIR / "audio.py").read_bytes(), AUDIO_PCM_SHA256, "maintained PCM audio.py"
    )
    old_version = original["faster_whisper/version.py"]
    if old_version.count(b'__version__ = "1.2.1"') != 1:
        raise ValueError("Unexpected upstream version.py.")
    files["faster_whisper/version.py"] = old_version.replace(
        b'__version__ = "1.2.1"', f'__version__ = "{VERSION}"'.encode("ascii")
    )
    metadata = original[f"{OLD_INFO}/METADATA"].decode("utf-8")
    if metadata.count("Version: 1.2.1\n") != 1 or metadata.count("Requires-Dist: av>=11\n") != 1:
        raise ValueError("Unexpected upstream dependency metadata.")
    metadata = metadata.replace("Version: 1.2.1\n", f"Version: {VERSION}\n", 1).replace(
        "Requires-Dist: av>=11\n", 'Provides-Extra: audio\nRequires-Dist: av>=11; extra == "audio"\n', 1
    )
    files[f"{NEW_INFO}/METADATA"] = metadata.encode("utf-8")
    wheel_metadata = files[f"{NEW_INFO}/WHEEL"].decode("utf-8")
    files[f"{NEW_INFO}/WHEEL"] = "\n".join(
        "Generator: LiveSubtitle build_pcm_whisper.py" if line.startswith("Generator:") else line
        for line in wheel_metadata.split("\n")
    ).encode("utf-8")
    provenance = {
        "version": VERSION,
        "upstream_url": UPSTREAM_URL,
        "upstream_sha256": UPSTREAM_SHA256,
        "upstream_source": "https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1",
        "license": "MIT; unchanged upstream LICENSE included",
        "changes": ["lazy PyAV import in audio.py", "optional audio dependency extra", "local version and RECORD"],
        "audio_sha256": AUDIO_PCM_SHA256,
        "unchanged_vad_sha256": VAD_SHA256,
        "unchanged_vad_asset_sha256": VAD_ASSET_SHA256,
    }
    files[f"{NEW_INFO}/livesubtitle-pcm.json"] = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8")
    files[f"{NEW_INFO}/RECORD"] = make_record(files, NEW_INFO)
    return files


def build_wheel(wheel_path: Path, output_dir: Path) -> Path:
    files = patched_files(upstream_files(wheel_path))
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / WHEEL_FILENAME
    # Stored entries, fixed timestamps and attributes make builds byte-identical
    # across Python/zlib versions. The small wheel is about 1.4 MB uncompressed.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 10, 5, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    destination.write_bytes(buffer.getvalue())
    destination.with_suffix(".whl.sha256").write_text(
        f"{sha256(buffer.getvalue())}  {destination.name}\n", encoding="ascii"
    )
    return destination


def apply_to_staging(wheel_path: Path, site_packages: Path) -> None:
    """Change only the validated package and its metadata in an explicit staging tree."""
    site = site_packages.resolve(strict=True)
    if site.name.lower() != "site-packages":
        raise ValueError("Expected an explicit site-packages staging directory.")
    working_site = APP_ROOT / "runtime/python/Lib/site-packages"
    if site == working_site.resolve():
        raise ValueError("Refusing to alter this app's working runtime; use a separate staging copy.")
    package = site / "faster_whisper"
    if package.is_symlink() or (hasattr(package, "is_junction") and package.is_junction()):
        raise ValueError("Refusing a linked faster_whisper package; staging must own its files.")
    if package.resolve().parent != site:
        raise ValueError("Package resolves outside staging.")
    original = upstream_files(wheel_path)
    files = patched_files(original)
    new_info = site / NEW_INFO
    old_info = site / OLD_INFO
    if new_info.exists():
        for name, data in files.items():
            if name == f"{NEW_INFO}/RECORD":
                continue
            if not (site / name).is_file() or (site / name).read_bytes() != data:
                raise ValueError(f"Existing local fork differs: {name}")
        if old_info.exists():
            raise ValueError("Both upstream and fork metadata exist; resolve manually.")
        return
    if not old_info.is_dir() or old_info.is_symlink() or (hasattr(old_info, "is_junction") and old_info.is_junction()):
        raise ValueError("Expected unlinked faster-whisper 1.2.1 metadata.")
    # Validate every upstream package file, including VAD. Never overwrite a
    # locally changed transcription/VAD implementation with a stock wheel.
    for name, data in original.items():
        if not name.startswith("faster_whisper/"):
            continue
        path = site / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise ValueError(f"Package differs from the audited input: {name}")
        if not path.resolve().is_relative_to(package.resolve()):
            raise ValueError(f"Linked file resolves outside the package: {name}")
    # Validate the exact old metadata too, rather than silently replacing a
    # different dependency declaration that happens to share the version.
    if (old_info / "METADATA").read_bytes() != original[f"{OLD_INFO}/METADATA"]:
        raise ValueError("Unexpected installed upstream METADATA.")
    for name, data in files.items():
        if name.startswith(NEW_INFO + "/") or name in ("faster_whisper/audio.py", "faster_whisper/version.py"):
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    (new_info / "INSTALLER").write_text("livesubtitle-pcm\n", encoding="ascii")
    installed_files = {name: data for name, data in files.items() if name != f"{NEW_INFO}/RECORD"}
    installed_files[f"{NEW_INFO}/INSTALLER"] = (new_info / "INSTALLER").read_bytes()
    (new_info / "RECORD").write_bytes(make_record(installed_files, NEW_INFO))
    # Exact checked metadata path only, never a wildcard or runtime-wide delete.
    if old_info.resolve().parent != site or old_info.name != OLD_INFO:
        raise ValueError("Refusing metadata cleanup outside staging.")
    shutil.rmtree(old_info)
    for name in ("audio", "version"):
        cache = package / "__pycache__"
        if cache.is_dir() and not cache.is_symlink() and not (hasattr(cache, "is_junction") and cache.is_junction()):
            for pyc in cache.glob(name + ".*.pyc"):
                pyc.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-wheel", type=Path, help="Offline input; SHA256 must match the pinned upstream wheel.")
    parser.add_argument("--output-dir", type=Path, default=APP_ROOT / ".dependency-build")
    parser.add_argument("--install", action="store_true", help="Install requirements with this Python after building the fork.")
    parser.add_argument("--apply-site-packages", type=Path, help="Apply to an owned release staging tree, preserving all other packages.")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    upstream = args.upstream_wheel
    if upstream is None:
        upstream = output / UPSTREAM_FILENAME
        if not upstream.exists():
            with urllib.request.urlopen(UPSTREAM_URL, timeout=60) as response:
                data = checked(response.read(), UPSTREAM_SHA256, "downloaded upstream wheel")
            upstream.write_bytes(data)
    result = build_wheel(upstream, output)
    print(f"Built {result.name}: {sha256(result.read_bytes())}")
    if args.apply_site_packages:
        apply_to_staging(upstream, args.apply_site_packages)
        print(f"Applied {VERSION} to {args.apply_site_packages}")
    if args.install:
        # requirements uses an app-relative generated wheel path. Keep the
        # standard source-build destination even with a custom artifact output.
        default_output = APP_ROOT / ".dependency-build"
        if result.resolve().parent != default_output.resolve():
            default_output.mkdir(exist_ok=True)
            shutil.copy2(result, default_output / result.name)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(APP_ROOT / "engine/requirements.txt")],
            cwd=APP_ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
