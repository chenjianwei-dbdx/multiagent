"""Renderers: deterministic DOCX output from a verified DocxRenderIR (P1).

The renderer is a pure function over its inputs: it performs no file IO and
never talks to the ArtifactStore; the calling pipeline owns persistence.
Slot content enters only through the ``resolve_span`` callback, which the
upper layer implements with ArtifactStore + ``verify_span`` — there is no
public API in this package that accepts free body text.
"""

from omas.renderers.docx_renderer import (
    DocxRenderer,
    RenderError,
    SpanTextResolver,
    render_docx,
)

__all__ = [
    "DocxRenderer",
    "RenderError",
    "SpanTextResolver",
    "render_docx",
]
