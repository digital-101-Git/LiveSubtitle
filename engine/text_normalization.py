"""Comparison-only Chinese script normalization using the Windows Unicode API.

Captions keep the recognizer's original text. On other platforms, or if the OS
conversion fails, comparison remains conservative and uses the original text.
"""
from functools import lru_cache
import ctypes
import os


def _load_mapper():
    if os.name != "nt":
        return None
    try:
        mapper = ctypes.WinDLL("kernel32", use_last_error=True).LCMapStringEx
        mapper.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_wchar_p,
                           ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int,
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t]
        mapper.restype = ctypes.c_int
        return mapper
    except (AttributeError, OSError):
        return None


_mapper = _load_mapper()


@lru_cache(maxsize=4096)
def chinese_comparison_text(text: str) -> str:
    if not text or _mapper is None or not any("\u3400" <= char <= "\u9fff" for char in text):
        return text
    # -1 includes the terminating NUL and avoids Python/UTF-16 length mismatch.
    try:
        size = _mapper("zh-CN", 0x02000000, text, -1, None, 0, None, None, 0)
        if size <= 0:
            return text
        output = ctypes.create_unicode_buffer(size)
        written = _mapper("zh-CN", 0x02000000, text, -1, output, size, None, None, 0)
        # Windows can map 麼 to 麽, while recognizers emit the modern simplified
        # variant 么. This orthographic equivalence does not merge homophones.
        return output.value.replace("麽", "么") if written else text
    except (OSError, ValueError):
        return text
