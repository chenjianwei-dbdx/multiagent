"""Property and edge-case tests for canonicalisation (T17 foundations).

Covers: NFC, CRLF/CR → LF, combining characters, emoji (incl. ZWJ sequences),
astral code points (code point counting vs bytes vs UTF-16 units), strict
UTF-8 decoding, and idempotence of canonicalisation.
"""

from __future__ import annotations

import hashlib
import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from omas.core.canonical import (
    canonicalize,
    find_illegal_control_chars,
    validate_renderable_text,
)
from omas.domain.errors import DecodeError

# Boundaries tuned to include CJK, emoji, combining marks, and C0 controls.
TEXT_ALPHABET = st.characters(
    min_codepoint=0x09,
    max_codepoint=0x2FA1D,
    exclude_categories=["Cs", "Cn"],
)


@given(st.text(alphabet=TEXT_ALPHABET, max_size=200))
def test_canonical_is_idempotent(raw: str) -> None:
    once = canonicalize(raw)
    twice = canonicalize(once.text)
    assert once.text == twice.text
    assert once.sha256 == twice.sha256


@given(st.text(alphabet=TEXT_ALPHABET, max_size=200))
def test_canonical_has_no_cr(raw: str) -> None:
    assert "\r" not in canonicalize(raw).text


@given(st.text(alphabet=TEXT_ALPHABET, max_size=200))
def test_canonical_is_nfc(raw: str) -> None:
    result = canonicalize(raw)
    lf_only = result.text.replace("\r\n", "\n").replace("\r", "\n")
    assert result.text == unicodedata.normalize("NFC", lf_only)


@given(st.text(alphabet=TEXT_ALPHABET, max_size=200))
def test_sha_over_utf8_encoding(raw: str) -> None:
    result = canonicalize(raw)
    assert result.sha256 == hashlib.sha256(result.text.encode("utf-8")).hexdigest()
    assert result.size_bytes == len(result.text.encode("utf-8"))


def test_crlf_and_lone_cr_become_lf() -> None:
    cr, lf = chr(13), chr(10)
    result = canonicalize("a" + cr + lf + "b" + cr + "c" + lf + cr + "d")
    # a CRLF b CR c LF CR d  ->  a LF b LF c LF LF d (no CR survives)
    assert result.text == "a" + lf + "b" + lf + "c" + lf + lf + "d"
    assert cr not in result.text


def test_combining_char_composes_under_nfc() -> None:
    # e + COMBINING ACUTE ACCENT -> single precomposed code point
    result = canonicalize("e\u0301")
    assert result.text == "\xe9"
    assert result.code_points == 1


def test_emoji_code_point_counting() -> None:
    # 4-byte UTF-8 astral emoji is ONE code point (not bytes, not UTF-16 units)
    single = canonicalize("😀")
    assert single.code_points == 1
    assert single.size_bytes == 4
    # family emoji = 4 emoji + 3 ZWJ = 7 code points
    family = canonicalize("👨‍👩‍👧‍👦")
    assert family.code_points == 7
    # astral plane char: 1 code point, 4 UTF-8 bytes, 2 UTF-16 units
    astral = canonicalize("\U00010000")
    assert astral.code_points == 1
    assert astral.size_bytes == 4


def test_nfc_does_not_touch_zwj_or_cjk() -> None:
    text = "中文测试👨‍👩‍👧‍👦한국어"
    assert canonicalize(text).text == text


def test_invalid_utf8_rejected() -> None:
    with pytest.raises(DecodeError):
        canonicalize(b"\xff\xfe broken")
    with pytest.raises(DecodeError):
        canonicalize(b"ok text then \x80")


def test_bytes_and_str_agree() -> None:
    text = "café\r\n😀"
    from_bytes = canonicalize(text.encode("utf-8"))
    from_str = canonicalize(text)
    assert from_bytes.sha256 == from_str.sha256


def test_control_char_detection() -> None:
    # \a (BEL) and \f (FF) trigger docxtpl rich-text control behaviour and
    # must be detected, never filtered (v1.1 §3.2).
    assert find_illegal_control_chars("ok\x07text") == (2,)
    assert find_illegal_control_chars("a\x0cb") == (1,)
    assert find_illegal_control_chars("\x1b[0m") == (0,)  # only ESC is a control
    # TAB / LF / CR are legal
    assert find_illegal_control_chars("\t\n\r") == ()
    with pytest.raises(ValueError, match="forbidden control characters"):
        validate_renderable_text("line1\x07line2")


def test_canonicalize_does_not_trim_or_collapse() -> None:
    raw = "  spaced   out\t\ttabbed  \n\n\n"
    assert canonicalize(raw).text == raw
