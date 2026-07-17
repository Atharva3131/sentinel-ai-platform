"""ChunkingEngine — splits a Document into overlapping text Chunks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.retrieval.exceptions import ChunkingError
from backend.retrieval.models import Chunk, ChunkStrategy, Document

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_BOUNDARY = re.compile(r"\n{2,}")


@dataclass(slots=True)
class ChunkingEngine:
    """Splits a Document into Chunks using a configurable strategy.

    ``chunk_size`` is the target size in characters for FIXED and the soft upper
    bound for SENTENCE / PARAGRAPH strategies.
    ``chunk_overlap`` is the number of trailing characters from the previous chunk
    that are prepended to each subsequent chunk.

    Strategies:
        FIXED     — sliding window of exactly ``chunk_size`` chars.
        SENTENCE  — splits on sentence boundaries; combines sentences up to
                    ``chunk_size`` chars before emitting a chunk.
        PARAGRAPH — splits on blank lines; combines paragraphs up to ``chunk_size``
                    chars before emitting a chunk.
        SEMANTIC  — not yet implemented; falls back to PARAGRAPH.
    """

    strategy: ChunkStrategy = ChunkStrategy.PARAGRAPH
    chunk_size: int = 512
    chunk_overlap: int = 64

    async def chunk(self, document: Document) -> list[Chunk]:
        """Return an ordered list of Chunks derived from the document's content."""
        if not document.content.strip():
            return []

        try:
            match self.strategy:
                case ChunkStrategy.FIXED:
                    return self._fixed_chunks(document)
                case ChunkStrategy.SENTENCE:
                    return self._sentence_chunks(document)
                case ChunkStrategy.PARAGRAPH | ChunkStrategy.SEMANTIC:
                    return self._paragraph_chunks(document)
                case _:
                    return self._paragraph_chunks(document)
        except Exception as exc:
            raise ChunkingError(
                f"Chunking failed for document '{document.document_id}': {exc}",
                document_id=document.document_id,
                strategy=str(self.strategy),
            ) from exc

    def _fixed_chunks(self, document: Document) -> list[Chunk]:
        content = document.content
        size = max(self.chunk_size, 1)
        overlap = min(self.chunk_overlap, size - 1)
        step = size - overlap
        chunks: list[Chunk] = []
        offset = 0
        index = 0
        while offset < len(content):
            segment = content[offset : offset + size]
            chunks.append(
                Chunk.make(
                    document.document_id,
                    segment,
                    index,
                    char_offset=offset,
                )
            )
            offset += step
            index += 1
        return chunks

    def _sentence_chunks(self, document: Document) -> list[Chunk]:
        sentences = _SENTENCE_BOUNDARY.split(document.content)
        return self._group_segments(document, sentences)

    def _paragraph_chunks(self, document: Document) -> list[Chunk]:
        paragraphs = _PARAGRAPH_BOUNDARY.split(document.content)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]
        return self._group_segments(document, paragraphs)

    def _group_segments(
        self, document: Document, segments: list[str]
    ) -> list[Chunk]:
        """Combine segments into chunks up to chunk_size, with overlap."""
        chunks: list[Chunk] = []
        current: list[str] = []
        current_len = 0
        index = 0
        overlap_text = ""

        for segment in segments:
            seg_len = len(segment)
            if current_len + seg_len > self.chunk_size and current:
                text = " ".join(current)
                if overlap_text:
                    text = overlap_text + " " + text
                chunks.append(Chunk.make(document.document_id, text.strip(), index))
                index += 1
                # keep tail of current content as overlap
                tail = " ".join(current)
                overlap_text = tail[-self.chunk_overlap :] if self.chunk_overlap else ""
                current = []
                current_len = 0
            current.append(segment)
            current_len += seg_len

        if current:
            text = " ".join(current)
            if overlap_text:
                text = overlap_text + " " + text
            chunks.append(Chunk.make(document.document_id, text.strip(), index))

        return chunks
