"""Shared address normalization for the offline index and its queries."""
from __future__ import annotations

import re
import unicodedata


def normalize_address(text: str) -> str:
    """Normalize Russian address notation without broadening house numbers.

    House identifiers stay single tokens: ``7``, ``70``, ``7к1`` and ``7/1``
    must not match each other. FTS's default tokenizer splits punctuation, so
    slash-separated house numbers use a searchable letter separator instead.
    """
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    # Abbreviated street types use the same tokens as complete names.
    for pattern, replacement in (
        (r"\b(?:просп(?:ект)?|пр-т|пр-кт)\b\.?", "проспект "),
        (r"\b(?:ул|улица)\b\.?", "улица "),
        (r"\b(?:пер|переулок)\b\.?", "переулок "),
        (r"\b(?:бул|б-р|бульвар)\b\.?", "бульвар "),
        (r"\b(?:наб|набережная)\b\.?", "набережная "),
        (r"\b(?:пл|площадь)\b\.?", "площадь "),
        (r"\b(?:ш|шоссе)\b\.?", "шоссе "),
        (r"\b(?:пр-зд|проезд)\b\.?", "проезд "),
    ):
        # Keep a separator after a consumed dot: ул.Тверская must not become
        # улицатверская. The boundary precedes the dot to avoid partial words.
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\b(?:город\b\s*|г\.\s*|г\s+)", " ", text)
    text = re.sub(r"\b(?:дом|д)\.?\s*(?=\d)", "", text)
    text = re.sub(r"(?<=\d)\s+([а-яa-z])\b", r"\1", text)
    # Join the primary house number and optional корпус / строение suffix.
    text = re.sub(
        r"(\d+[а-яa-z]?)\s*[,;]?\s*(?:корпус|корп|к)\.?\s*(?=\d)", r"\1к", text,
    )
    text = re.sub(
        r"(\d+[а-яa-z]?)\s*[,;]?\s*(?:строение|стр|с)\.?\s*(?=\d)", r"\1с", text,
    )
    text = re.sub(r"(?<=\d)\s*/\s*(?=\d)", "дробь", text)
    text = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "дефис", text)
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))
