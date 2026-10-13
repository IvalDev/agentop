"""Fixed-size character chunking with paragraph-aware split points.

Chunks feed the embedding pipeline in T5, so the contract here is
deliberately simple and pure: split ``text`` into windows of at most
``chunk_size`` characters where consecutive windows share an ``overlap``
character suffix/prefix. Split points snap to paragraph (``\\n\\n``) or
line (``\\n``) boundaries when one sits in the fuller half of the window,
because retrieval cites verbatim text and mid-word cuts read badly.
"""

_PARAGRAPH_SEP = "\n\n"
_LINE_SEP = "\n"


def _snap_end(text: str, start: int, end: int, chunk_size: int, overlap: int) -> int:
    """Return the best split point in ``text[start:end]``.

    A boundary is accepted only when it leaves room to advance: the next
    window starts at ``end - overlap``, which must stay past ``start``.
    """
    min_end = start + max(overlap + 1, chunk_size // 2)
    for separator in (_PARAGRAPH_SEP, _LINE_SEP):
        index = text.rfind(separator, start + 1, end)
        if index != -1 and index >= min_end:
            return index
    return end


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """Split ``text`` into overlapping chunks of at most ``chunk_size`` chars.

    Returns ``[]`` for an empty string and ``[text]`` when the whole text
    already fits in one window. Raises ``ValueError`` when ``overlap`` is
    not a non-negative count strictly smaller than ``chunk_size`` (such a
    window could never advance).
    """
    if not 0 <= overlap < chunk_size:
        raise ValueError(f"overlap must satisfy 0 <= overlap < chunk_size, got {overlap}")
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        if end < length:
            end = _snap_end(text, start, end, chunk_size, overlap)
        chunks.append(text[start:end])
        if end >= length:
            # The final chunk already reaches the end of the text; nothing
            # remains past its overlap suffix, so advancing would stall.
            break
        start = end - overlap
    return chunks
