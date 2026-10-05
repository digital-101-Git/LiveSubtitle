"""Confined, nonrecursive access to files in the application's logs directory."""
from pathlib import Path
import stat


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def log_directory(root: Path, *, create: bool = False) -> Path:
    directory = Path(root).resolve() / "logs"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if not create:
            return directory
        directory.mkdir(exist_ok=True)
        info = directory.lstat()
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError("로그 폴더가 일반 디렉터리가 아닙니다.")
    return directory


def regular_log_path(directory: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise OSError("로그 파일 이름이 올바르지 않습니다.")
    path = directory / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise OSError("로그 경로가 일반 파일이 아닙니다.")
    return path


def delete_log_files(root: Path) -> dict:
    """Caller holds the log writer lock. Never traverses a child directory/link."""
    directory = log_directory(root)
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return {"ok": True, "deleted_count": 0, "failed_files": []}
    deleted, failed = 0, []
    for entry in entries:
        try:
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode) and not _is_link(info):
                continue
            regular_log_path(directory, entry.name).unlink(missing_ok=True)
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError:
            failed.append(entry.name)
    return {"ok": not failed, "deleted_count": deleted, "failed_files": sorted(failed)}
