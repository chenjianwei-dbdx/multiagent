"""Standalone OOXML extraction for OMAS (v1.1 §7, §9).

Pure-lxml extraction of the supported template subset plus streaming ZIP
safety checks and effective-style resolution. This package never imports
python-docx — it is the independent evidence source gates must rely on.
"""

from .extract import (
    BREAK,
    TAB,
    TEXT,
    DocumentDetail,
    ExtractedDocument,
    ExtractedParagraph,
    ExtractedPart,
    ParagraphDetail,
    TextToken,
    TextTokenKind,
    document_plain_text,
    extract_details,
    extract_docx,
    find_unsupported_text_containers,
    paragraph_plain_text,
)
from .styles import ResolvedStyle, StyleResolver
from .zipcheck import check_zip_safety

__all__ = [
    "BREAK",
    "TAB",
    "TEXT",
    "DocumentDetail",
    "ExtractedDocument",
    "ExtractedParagraph",
    "ExtractedPart",
    "ParagraphDetail",
    "ResolvedStyle",
    "StyleResolver",
    "TextToken",
    "TextTokenKind",
    "check_zip_safety",
    "document_plain_text",
    "extract_details",
    "extract_docx",
    "find_unsupported_text_containers",
    "paragraph_plain_text",
]
