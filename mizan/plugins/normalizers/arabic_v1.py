"""arabic_v1 — Appendix C.1, applied in order. Rules are printed in every report."""
from __future__ import annotations

import re
import string
import unicodedata

from mizan.plugins.base import Normalizer

_DIACRITICS = re.compile("[ً-ْٰ]")
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
_TA_YA = str.maketrans({"ة": "ه", "ى": "ي"})
_HAMZA = str.maketrans({"ؤ": "و", "ئ": "ي"})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ASCII_PUNCT = "".join(c for c in string.punctuation if c != "'")
_PUNCT = str.maketrans({c: " " for c in "،؛؟" + _ASCII_PUNCT})
_STRAY_APOS = re.compile(r"(?<![A-Za-z])'|'(?![A-Za-z])")


class ArabicV1(Normalizer):
    kind = "arabic_v1"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        return {
            "type": "object",
            "properties": {
                "hamza_on_waw_yaa": {
                    "type": "boolean", "default": True,
                    "description": "Fold ؤ→و and ئ→ي (rule 4).",
                }
            },
        }

    def normalize(self, text: str) -> str:
        t = unicodedata.normalize("NFC", text or "")                    # 1
        t = t.replace("ـ", "")                                      # 2 tatweel
        t = _DIACRITICS.sub("", t)                                       # 2 harakat
        t = t.translate(_ALEF)                                           # 3
        t = t.translate(_TA_YA)                                          # 4
        if self.config.get("hamza_on_waw_yaa", True):
            t = t.translate(_HAMZA)                                      # 4 (configurable)
        t = t.translate(_DIGITS)                                         # 5
        t = t.translate(_PUNCT)                                          # 6
        t = _STRAY_APOS.sub(" ", t)                                      # 6 keep intra-token apostrophes
        t = t.lower()                                                    # 7
        return " ".join(t.split())                                       # 8

    def rules(self) -> list[str]:
        r = [
            "1. Unicode NFC.",
            "2. Remove tatweel (U+0640) and harakat/diacritics [U+064B-U+0652, U+0670].",
            "3. Alef variants أ إ آ ٱ → ا.",
            "4. ة → ه; ى → ي"
            + ("; ؤ → و; ئ → ي." if self.config.get("hamza_on_waw_yaa", True) else " (hamza folding disabled)."),
            "5. Arabic-Indic (٠-٩) and Persian (۰-۹) digits → ASCII digits.",
            "6. Arabic punctuation ،؛؟ and all ASCII punctuation → space; intra-token apostrophes in Latin words are kept.",
            "7. Latin → lowercase.",
            "8. Collapse whitespace; strip.",
            "9. Number words ↔ digits are NOT converted.",
        ]
        return r
