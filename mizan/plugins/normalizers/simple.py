from __future__ import annotations

import re
import unicodedata

from mizan.plugins.base import Normalizer


class NoneNormalizer(Normalizer):
    """Identity (whitespace-trimmed). Raw text is always stored; this only affects scoring."""
    kind = "none"
    version = "1.0.0"

    def normalize(self, text: str) -> str:
        return " ".join(unicodedata.normalize("NFC", text or "").split())

    def rules(self) -> list[str]:
        return ["Unicode NFC and whitespace collapse only; no other normalization."]


class WhisperBasic(Normalizer):
    """Approximation of Whisper's BasicTextNormalizer: lowercase, drop bracketed text, punctuation → space."""
    kind = "whisper_basic"
    version = "1.0.0"

    def normalize(self, text: str) -> str:
        t = unicodedata.normalize("NFKC", text or "").lower()
        t = re.sub(r"[<\[][^>\]]*[>\]]", " ", t)
        t = re.sub(r"\([^)]*\)", " ", t)
        t = "".join(" " if unicodedata.category(c)[0] in "PS" and c != "'" else c for c in t)
        return " ".join(t.split())

    def rules(self) -> list[str]:
        return [
            "NFKC, lowercase.",
            "Remove text inside [], <>, ().",
            "Unicode punctuation and symbols → space (apostrophes kept).",
            "Collapse whitespace.",
        ]
