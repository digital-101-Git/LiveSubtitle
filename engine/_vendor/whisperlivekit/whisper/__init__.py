# Modified for LiveSubtitle by digital-101 (2026-10-05): reduced package initialization to avoid unrelated imports and automatic downloads.
# See UPSTREAM.json for pinned source and changes. Original licenses retained.
"""Local decoder APIs only: no automatic checkpoint downloads."""
from .decoding import DecodingOptions
from . import tokenizer
