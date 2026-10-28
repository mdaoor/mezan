"""Script/language helpers shared by metrics and services. Pure functions, no I/O."""
from __future__ import annotations

import re
import unicodedata

_AR = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LAT = re.compile(r"[A-Za-zÀ-ɏ]")


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def script_of(token: str) -> str:
    """'ar' if the token holds any Arabic-script letter, 'en' for Latin, else 'other'."""
    if _AR.search(token):
        return "ar"
    if _LAT.search(token):
        return "en"
    return "other"


def infer_spans(text: str) -> list[dict]:
    """Language spans over ``text`` by script of each whitespace token (adjacent same-lang tokens merge)."""
    spans: list[dict] = []
    for m in re.finditer(r"\S+", text or ""):
        lang = script_of(m.group())
        if lang == "other":
            # digits/punctuation inherit the neighbouring span
            if spans:
                spans[-1]["end_char"] = m.end()
            continue
        if spans and spans[-1]["lang"] == lang:
            spans[-1]["end_char"] = m.end()
        else:
            spans.append({"start_char": m.start(), "end_char": m.end(), "lang": lang})
    return spans


def lang_mix(text: str, spans: list[dict] | None = None) -> str:
    langs = {s["lang"] for s in (spans if spans else infer_spans(text)) if s.get("lang") in ("ar", "en")}
    if not langs:
        return "ar" if _AR.search(text or "") else ("en" if _LAT.search(text or "") else "none")
    return "mixed" if len(langs) > 1 else next(iter(langs))


def duration_bucket(seconds: float) -> str:
    if seconds < 5:
        return "<5s"
    if seconds < 15:
        return "5-15s"
    if seconds <= 30:
        return "15-30s"
    return ">30s"
