"""Tests for the shared hex-blob decode primitive (#120).

``decode_hex_or_str`` was hoisted out of CursorExtractor so future
SQLite-backed extractors can reuse the heuristic instead of re-deriving
blob decoding per tool.
"""

from __future__ import annotations

import pytest

from lore.utils.text_processing import decode_hex_or_str


def test_plain_string_passes_through():
    assert decode_hex_or_str("Hello, world!") == "Hello, world!"


def test_non_string_returns_empty():
    assert decode_hex_or_str(None) == ""
    assert decode_hex_or_str(b"raw-bytes") == ""
    assert decode_hex_or_str(42) == ""


def test_hex_encoded_utf8_is_decoded():
    payload = "Mitochondrien erstellen Energie"
    encoded = payload.encode("utf-8").hex()
    assert decode_hex_or_str(encoded) == payload


def test_short_hex_like_string_is_not_decoded():
    # <= 10 chars never passes the length heuristic.
    assert decode_hex_or_str("abcdef1234") == "abcdef1234"


def test_hex_with_space_in_first_50_is_not_decoded():
    value = "a b" + "c" * 60
    assert decode_hex_or_str(value) == value


def test_hex_with_non_hex_char_in_first_100_is_not_decoded():
    value = "g" + "a" * 120
    assert decode_hex_or_str(value) == value


def test_hex_heuristic_match_but_invalid_hex_returns_original():
    # Passes the prefix heuristic but contains an odd-length/non-hex tail,
    # so bytes.fromhex must fail and the original comes back (ValueError
    # path — must not raise).
    value = "a" * 100 + "z" * 5
    assert decode_hex_or_str(value) == value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("48656c6c6f20776f726c64", "Hello world"),  # > 10 chars, decodes
        ("", ""),  # empty → fails length heuristic, passes through
    ],
)
def test_table_cases(raw, expected):
    assert decode_hex_or_str(raw) == expected
