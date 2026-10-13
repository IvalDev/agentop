"""Synchronous text extraction from raw document bytes.

Worker-side helper: CPU-bound parsers run inside the ingest worker (T12
queue), so these functions are deliberately synchronous. Content types are
matched after stripping any ``; charset=...`` suffix from the upload's
MIME type.
"""

from io import BytesIO

from docx import Document as DocxDocument
from pypdf import PdfReader

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PLAIN_TYPES = frozenset({"text/plain", "text/markdown"})


class UnsupportedFormatError(Exception):
    """Raised when no extractor is registered for a content type."""


def extract_text(data: bytes, content_type: str) -> str:
    """Return the text content of ``data`` for a known ``content_type``.

    Raises :class:`UnsupportedFormatError` for unknown types and lets
    parser errors (corrupt PDF/DOCX) propagate to the caller, which marks
    the document as failed.
    """
    mime = content_type.split(";")[0].strip().lower()
    if mime in _PLAIN_TYPES:
        return data.decode("utf-8", errors="replace")
    if mime == "application/pdf":
        reader = PdfReader(BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if mime == DOCX_CONTENT_TYPE:
        document = DocxDocument(BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    raise UnsupportedFormatError(f"unsupported content type: {content_type or '(empty)'}")
