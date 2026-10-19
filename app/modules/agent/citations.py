"""Citation bookkeeping for one agent run.

Every passage shown to the model gets a stable number (first come, first
numbered; the same chunk keeps its number across searches). The model cites
with ``[n]`` markers; :meth:`CitationIndex.cited` returns only the passages
whose markers appear in the answer, so an invented number resolves to
nothing. Each citation carries ``document_id`` + character offsets into the
stored document text (acceptance criterion 3).
"""

import re
from uuid import UUID

from packages.contracts.schemas import ChatCitation, SearchHit

#: ``[2]`` or grouped ``[1, 3]``; at most four digits per number.
_MARKER = re.compile(r"\[(\d{1,4}(?:\s*,\s*\d{1,4})*)\]")


def cited_numbers(answer: str) -> list[int]:
    """Distinct marker numbers in ``answer``, in order of first use."""
    numbers: list[int] = []
    for match in _MARKER.finditer(answer):
        for part in match.group(1).split(","):
            number = int(part)
            if number not in numbers:
                numbers.append(number)
    return numbers


class CitationIndex:
    """Numbers passages as they are shown to the model and resolves markers."""

    def __init__(self) -> None:
        self._by_chunk: dict[UUID, ChatCitation] = {}

    def add(self, hit: SearchHit) -> int:
        """Return the number of ``hit``'s chunk, assigning the next one if new."""
        existing = self._by_chunk.get(hit.chunk_id)
        if existing is not None:
            return existing.number
        citation = ChatCitation(
            number=len(self._by_chunk) + 1,
            document_id=hit.document_id,
            document_filename=hit.document_filename,
            chunk_id=hit.chunk_id,
            chunk_index=hit.chunk_index,
            start_offset=hit.start_offset,
            end_offset=hit.end_offset,
            content=hit.content,
        )
        self._by_chunk[hit.chunk_id] = citation
        return citation.number

    def sources(self) -> list[ChatCitation]:
        """Every numbered passage, in number order."""
        return list(self._by_chunk.values())

    def cited(self, answer: str) -> list[ChatCitation]:
        """Passages whose markers appear in ``answer`` (unknown markers dropped)."""
        by_number = {citation.number: citation for citation in self._by_chunk.values()}
        return [by_number[number] for number in cited_numbers(answer) if number in by_number]
