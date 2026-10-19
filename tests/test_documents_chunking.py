"""Pure-function tests for chunking and synchronous text extraction."""

from io import BytesIO

import pytest
from docx import Document as DocxDocument
from pypdf import PdfWriter

from app.modules.documents.chunking import chunk_text
from app.modules.documents.extract import UnsupportedFormatError, extract_text


def test_empty_string_produces_no_chunks() -> None:
    """An empty document has nothing to chunk."""
    assert chunk_text("") == []


def test_text_under_chunk_size_stays_in_one_chunk() -> None:
    """Text shorter than the chunk size is returned unchanged in one chunk."""
    text = "Short note about the quarterly invoice."
    chunks = chunk_text(text, chunk_size=1000, overlap=200)
    assert chunks == [(text, 0, len(text))]


def test_long_text_produces_multiple_chunks_with_overlap() -> None:
    """Long text splits into several chunks and neighbours overlap by ``overlap``."""
    paragraphs = [f"Paragraph {i}: " + ("lorem ipsum " * 20) for i in range(12)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, chunk_size=400, overlap=80)
    texts = [chunk for chunk, _, _ in chunks]

    assert len(chunks) > 1
    assert all(len(chunk) <= 400 for chunk in texts)
    for previous, current in zip(texts, texts[1:], strict=False):
        assert previous[-80:] == current[:80]


def test_chunks_reassemble_to_cover_original_content() -> None:
    """Dropping each chunk's overlap from its predecessor rebuilds the input."""
    text = "\n\n".join(f"Section {i} " + ("data " * 40) for i in range(20))
    chunks = chunk_text(text, chunk_size=500, overlap=100)
    texts = [chunk for chunk, _, _ in chunks]

    rebuilt = texts[0] + "".join(chunk[100:] for chunk in texts[1:])
    assert rebuilt == text


def test_chunk_offsets_slice_the_original_text_exactly() -> None:
    """Every (start, end) pair reproduces its chunk from the source text."""
    text = "\n\n".join(f"Section {i}: " + ("contenido " * 50) for i in range(15))
    chunks = chunk_text(text, chunk_size=300, overlap=60)

    assert len(chunks) > 1
    for chunk, start, end in chunks:
        assert text[start:end] == chunk


def test_offsets_advance_and_terminal_chunk_reaches_text_end() -> None:
    """Offsets strictly advance; the last chunk ends exactly at the text end."""
    text = "\n\n".join(f"Section {i}: " + ("contenido " * 50) for i in range(15))
    chunks = chunk_text(text, chunk_size=300, overlap=60)

    starts = [start for _, start, _ in chunks]
    assert starts == sorted(starts)
    assert all(later > earlier for earlier, later in zip(starts, starts[1:], strict=False))
    assert chunks[0][1] == 0
    assert chunks[-1][2] == len(text)


def test_rejects_overlap_not_smaller_than_chunk_size() -> None:
    """A window cannot advance when the overlap swallows the whole chunk."""
    with pytest.raises(ValueError, match="overlap"):
        chunk_text("some text", chunk_size=100, overlap=100)


def test_extract_plain_text_decodes_utf8() -> None:
    """Plain text is decoded as UTF-8."""
    assert extract_text("factura número 42".encode(), "text/plain") == "factura número 42"


def test_extract_markdown_decodes_utf8() -> None:
    """Markdown is treated like plain text and decoded as UTF-8."""
    assert (
        extract_text("# Título\n\ncontenido".encode(), "text/markdown") == "# Título\n\ncontenido"
    )


def test_extract_replaces_invalid_utf8_bytes() -> None:
    """Undecodable bytes become U+FFFD instead of raising."""
    result = extract_text(b"ok\xffend", "text/plain")
    assert result == "ok�end"


def test_extract_strips_charset_suffix_from_content_type() -> None:
    """A ``text/plain; charset=utf-8`` header still hits the plain-text path."""
    assert extract_text(b"hola", "text/plain; charset=utf-8") == "hola"


def test_extract_unsupported_format_raises() -> None:
    """Unknown content types raise UnsupportedFormatError, not a crash."""
    with pytest.raises(UnsupportedFormatError, match="zip"):
        extract_text(b"PK", "application/zip")


def _minimal_pdf(text: str) -> bytes:
    """Build a one-page PDF containing ``text`` using pypdf's writer."""
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 50 200 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_extract_pdf_reads_page_text() -> None:
    """A minimal PDF's page text is extracted via pypdf."""
    data = _minimal_pdf("Hello PDF")
    assert "Hello PDF" in extract_text(data, "application/pdf")


def _minimal_docx(paragraph: str) -> bytes:
    """Build an in-memory .docx containing one paragraph."""
    document = DocxDocument()
    document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_extract_docx_reads_paragraph_text() -> None:
    """A python-docx document's paragraph text is extracted."""
    data = _minimal_docx("Hola oficina")
    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert extract_text(data, content_type) == "Hola oficina"
