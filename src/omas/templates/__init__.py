"""Template contract extraction and versioned registration (v1.1 §3)."""

from .extractor import EXTRACTOR_VERSION, build_contract, canonical_json
from .registry import (
    CONTRACT_FILE,
    DOCX_FILE,
    MANIFEST_FILE,
    STATIC_MAP_FILE,
    STYLES_FILE,
    TemplateRegistry,
)

__all__ = [
    "CONTRACT_FILE",
    "DOCX_FILE",
    "EXTRACTOR_VERSION",
    "MANIFEST_FILE",
    "STATIC_MAP_FILE",
    "STYLES_FILE",
    "TemplateRegistry",
    "build_contract",
    "canonical_json",
]
